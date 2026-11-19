"""Rebuild a sqlfluff TemplatedFile from a dbt model source and its dbt-oss compiled output.

sqlfluff's JinjaTracer finds which raw slices ran by re-rendering the template. That needs the
real dbt context. Here the raw slices come from the same JinjaAnalyzer lexer, and the execution
trace comes from matching the slice tree against the compiled SQL instead.
"""

from dataclasses import dataclass, field
from typing import Optional

import regex
from jinja2 import Environment
from sqlfluff.core.templaters.base import RawFileSlice, TemplatedFile, TemplatedFileSlice
from sqlfluff.core.templaters.slicers.tracer import JinjaAnalyzer

_ENV = Environment(extensions=["jinja2.ext.do", "jinja2.ext.loopcontrols"], keep_trailing_newline=True)
_OPAQUE = ("set", "macro", "call", "block")


@dataclass
class Node:
    kind: str
    idx: int = -1
    children: list = field(default_factory=list)
    branches: list = field(default_factory=list)
    end_idx: int = -1
    gid: str = ""


class SliceError(Exception):
    """The compiled output could not be aligned with the source template."""


def raw_slices(source: str) -> list[RawFileSlice]:
    analyzer = JinjaAnalyzer(source, _ENV)
    analyzer.analyze(render_func=lambda s: s)
    return analyzer.raw_sliced


def _strip_flags(rs: list[RawFileSlice]) -> list[bool]:
    """True for whitespace-only literals that Jinja's `-` whitespace control removes."""
    flags = [False] * len(rs)
    for i, s in enumerate(rs):
        if s.slice_type != "literal" or s.raw.strip():
            continue
        nxt = rs[i + 1].raw if i + 1 < len(rs) else ""
        prv = rs[i - 1].raw if i > 0 else ""
        if nxt[:3] in ("{%-", "{{-", "{#-") or prv.rstrip().endswith(("-%}", "-}}", "-#}")):
            flags[i] = True
    return flags


def build_tree(rs: list[RawFileSlice]) -> list[Node]:
    pos = 0

    def parse(stop: tuple) -> list[Node]:
        nonlocal pos
        nodes: list[Node] = []
        while pos < len(rs):
            s = rs[pos]
            tag = s.tag
            if s.slice_type in ("block_mid", "block_end") and tag in stop:
                return nodes
            if s.slice_type == "block_start" and tag == "if":
                node = Node("if", idx=pos)
                pos += 1
                node.branches.append((node.idx, parse(("elif", "else", "endif"))))
                while rs[pos].tag in ("elif", "else"):
                    tag_idx = pos
                    pos += 1
                    node.branches.append((tag_idx, parse(("elif", "else", "endif"))))
                node.end_idx = pos
                pos += 1
                nodes.append(node)
            elif s.slice_type == "block_start" and tag == "for":
                node = Node("for", idx=pos)
                pos += 1
                node.children = parse(("endfor",))
                node.end_idx = pos
                pos += 1
                nodes.append(node)
            elif s.slice_type == "block_start" and tag in _OPAQUE:
                node = Node("opaque", idx=pos)
                depth = 1
                pos += 1
                while depth:
                    if rs[pos].slice_type == "block_start" and rs[pos].tag in _OPAQUE:
                        depth += 1
                    elif rs[pos].slice_type == "block_end" and rs[pos].tag in tuple("end" + t for t in _OPAQUE):
                        depth -= 1
                    pos += 1
                node.end_idx = pos - 1
                nodes.append(node)
            elif s.slice_type == "literal":
                nodes.append(Node("literal", idx=pos))
                pos += 1
            elif s.slice_type == "comment" or (s.slice_type == "templated" and tag in ("set", "do")):
                nodes.append(Node("comment", idx=pos))
                pos += 1
            else:
                nodes.append(Node("templated", idx=pos))
                pos += 1
        return nodes

    tree = parse(())
    if pos != len(rs):
        raise SliceError(f"unbalanced template blocks at raw slice {pos}")
    return tree


def to_pattern(nodes: list[Node], rs: list[RawFileSlice], strip: list[bool], counter: list[int]) -> str:
    out = []
    for n in nodes:
        counter[0] += 1
        n.gid = f"g{counter[0]}"
        if n.kind == "literal":
            text = rs[n.idx].raw
            body = "" if strip[n.idx] else regex.escape(text)
            out.append(f"(?P<{n.gid}>{body})")
        elif n.kind == "templated":
            out.append(f"(?P<{n.gid}>.*?)")
        elif n.kind in ("comment", "opaque"):
            out.append(f"(?P<{n.gid}>)")
        elif n.kind == "if":
            alts = []
            for b, (_, body) in enumerate(n.branches):
                alts.append(f"(?P<{n.gid}b{b}>{to_pattern(body, rs, strip, counter)})")
            has_else = any(rs[t].tag == "else" for t, _ in n.branches)
            if not has_else:
                alts.append(f"(?P<{n.gid}none>)")
            out.append(f"(?P<{n.gid}>(?:{'|'.join(alts)}))")
        elif n.kind == "for":
            body = to_pattern(n.children, rs, strip, counter)
            out.append(f"(?P<{n.gid}>(?:(?P<{n.gid}it>{body}))*)")
    return "".join(out)


class _Cursor:
    def __init__(self, m: "regex.Match"):
        self.m = m
        self.used: dict[str, int] = {}

    def take(self, gid: str, lo: int, hi: int) -> Optional[tuple[int, int]]:
        spans = self.m.spans(gid)
        k = self.used.get(gid, 0)
        if k < len(spans) and lo <= spans[k][0] and spans[k][1] <= hi:
            self.used[gid] = k + 1
            return spans[k]
        return None


def build_sliced_file(source: str, compiled: str, rs: list[RawFileSlice], tree: list[Node]):
    strip = _strip_flags(rs)
    pattern = to_pattern(tree, rs, strip, [0])
    m = regex.fullmatch(pattern, compiled, flags=regex.DOTALL, timeout=20)
    if m is None:
        raise SliceError("compiled output does not match the source template structure")
    cur = _Cursor(m)
    sliced: list[TemplatedFileSlice] = []

    def src_slice(k: int) -> slice:
        stop = rs[k + 1].source_idx if k + 1 < len(rs) else len(source)
        return slice(rs[k].source_idx, stop)

    def rec(k: int, a: int, b: int) -> None:
        sliced.append(TemplatedFileSlice(rs[k].slice_type, src_slice(k), slice(a, b)))

    def walk(nodes: list[Node], lo: int, hi: int) -> int:
        p = lo
        for n in nodes:
            span = cur.take(n.gid, p, hi)
            if span is None:
                raise SliceError(f"lost track at raw slice {n.idx}")
            a, b = span
            if n.kind in ("literal", "templated", "comment"):
                rec(n.idx, a, b)
            elif n.kind == "opaque":
                for k in range(n.idx, n.end_idx + 1):
                    rec(k, a, a)
            elif n.kind == "if":
                rec(n.idx, a, a)
                taken = None
                for bi, (tag_idx, body) in enumerate(n.branches):
                    bspan = cur.take(f"{n.gid}b{bi}", a, b)
                    if bspan is not None:
                        taken = (bi, tag_idx, body, bspan)
                        break
                if taken is None:
                    if cur.take(f"{n.gid}none", a, b) is None:
                        raise SliceError(f"no branch matched at raw slice {n.idx}")
                else:
                    bi, tag_idx, body, bspan = taken
                    if bi > 0:
                        rec(tag_idx, a, a)
                    walk(body, bspan[0], bspan[1])
                rec(n.end_idx, b, b)
            elif n.kind == "for":
                rec(n.idx, a, a)
                q, iterations = a, 0
                while True:
                    it = cur.take(f"{n.gid}it", q, b)
                    if it is None:
                        break
                    walk(n.children, it[0], it[1])
                    rec(n.end_idx, it[1], it[1])
                    iterations += 1
                    q = it[1]
                    if it[0] == it[1]:
                        break
                if not iterations:
                    rec(n.end_idx, b, b)
            p = b
        return p

    end = walk(tree, 0, len(compiled))
    if end != len(compiled):
        raise SliceError("trace did not cover the whole compiled output")
    return sliced


def restore_trailing_newlines(source: str, compiled: str) -> str:
    """dbt renders with keep_trailing_newline=False, which drops exactly one final newline; put it back.

    Same rule as sqlfluff-templater-dbt. Verified on dbt-oss 2.0.11: "a\\n\\n\\n" compiles to "a\\n\\n".
    """
    if source.endswith("\n") and not source.rstrip().endswith("-%}"):
        return compiled + "\n"
    return compiled


def build_templated_file(source: str, compiled: str, fname: str) -> TemplatedFile:
    compiled = restore_trailing_newlines(source, compiled)
    rs = raw_slices(source)
    tree = build_tree(rs)
    sliced = build_sliced_file(source, compiled, rs, tree)
    return TemplatedFile(source_str=source, fname=fname, templated_str=compiled, sliced_file=sliced, raw_sliced=rs)
