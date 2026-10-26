"""Muted alerts: config/alert-mutes.yaml says which alerts are not posted to Slack.

The file is read again whenever it changes (no restart). A muted alert is still kept in the
history ("muted by AL-001"). Each entry can be turned off with `enabled: false`, and the Alerts
page has a switch that pauses every mute at once. If the file has a mistake, the last good
version stays in use and the Alerts page shows the error.

    mutes:
      - id: AL-001
        enabled: true
        title: Missing per-store ES index
        owner: Rahul
        reason: Known, being fixed
        match:                       # every field given must match; a list = any of them
          rules: [lb_status]         # alert types; leave out for all
          targetGroups: [tg-alpha-edds-*]
          clusters: [bigcom-*]
          subjects: ["post-tp-01 / *"]
          paths: ["POST /fenixdelest/api/v3/deliveryestimates", "GET /.aws/*"]
          codes: [400, 5xx]
          signatures: ["conversion_guided_transit_time_info"]   # text in the service's ERROR lines
          healthReasons: [Elb.RegistrationInProgress]           # target health reasons
          converterErrors: [OutOfMemory]                         # text in the converter's error

What each kind of entry does
- targetGroups / clusters / subjects only: that alert is not posted for those names.
- paths (optionally with codes): those requests are not counted by the load balancer rules.
- signatures: the load balancer rule is not posted when every ERROR line the service wrote in the
  same minutes is one of these (any other error, or no error lines at all, and it is posted);
  the application-log rule doesn't count those lines.
- healthReasons: targets in these states don't count as unhealthy.
- converterErrors: converter errors with this text don't alert.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("vector_logs.alerts")

MATCH_KEYS = {"rules", "targetGroups", "clusters", "subjects", "paths", "codes", "signatures", "signature",
              "healthReasons", "converterErrors"}
ENTRY_KEYS = {"id", "enabled", "title", "owner", "reason", "notes", "match"}
LB_TYPES = ("lb_errors", "lb_status", "lb_latency")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,39}$")
CODE_RE = re.compile(r"^[1-5](\d\d|xx)$")
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT", "TRACE", "-"}


class MuteFileError(ValueError):
    pass


def _glob(pattern: str) -> re.Pattern:
    """Shell-style * and ?, case-insensitive; <store>, <uuid>, <id>, <n> … also match any one part."""
    parts = re.split(r"(<[^>/]+>)", pattern)
    rx = "".join(r"(?:<[^>/]+>|[^/]+)" if re.fullmatch(r"<[^>/]+>", p) else fnmatch.translate(p)[4:-3]
                 for p in parts if p)
    return re.compile(rf"(?is)\A{rx}\Z")


def code_overlap(a: list[str], b: list[str]) -> bool:
    """Do two code lists share a code? '4xx' covers '400'."""
    for x in a:
        for y in b:
            if x == y or (x.endswith("xx") and y[0] == x[0]) or (y.endswith("xx") and x[0] == y[0]):
                return True
    return False


def code_in(code: int, codes: list[str]) -> bool:
    s = str(code)
    return any(c == s or (c.endswith("xx") and s[0] == c[0]) for c in codes)


@dataclass
class Mute:
    id: str
    enabled: bool = True
    title: str = ""
    owner: str = ""
    reason: str = ""
    rules: list[str] | None = None
    target_groups: list[str] = field(default_factory=list)
    clusters: list[str] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)
    paths: list[tuple[str | None, str]] = field(default_factory=list)      # (method or None, pattern)
    codes: list[str] = field(default_factory=list)
    signatures: list[str] = field(default_factory=list)
    health_reasons: list[str] = field(default_factory=list)
    converter_errors: list[str] = field(default_factory=list)
    match: dict = field(default_factory=dict)                              # as written, for the page

    # -- what kind of entry
    @property
    def kind(self) -> str:
        if self.signatures:
            return "signature"
        if self.paths:
            return "path"
        if self.health_reasons:
            return "health"
        if self.converter_errors:
            return "converter"
        return "subject"

    def for_rule(self, rtype: str) -> bool:
        return self.rules is None or rtype in self.rules

    @staticmethod
    def _any(name: str | None, patterns: list[str]) -> bool:
        return bool(name) and any(_glob(p).match(name) for p in patterns)

    def tg_ok(self, tg: str | None) -> bool:
        """The target group scope (no targetGroups / clusters = any)."""
        if not self.target_groups and not self.clusters:
            return True
        cluster = tg[3:] if tg and tg.startswith("tg-") else tg
        return self._any(tg, self.target_groups) or self._any(cluster, self.clusters)

    def cluster_ok(self, cluster: str | None) -> bool:
        """The cluster scope for ECS / log rules: clusters, or a target group named tg-<cluster>."""
        if not self.target_groups and not self.clusters:
            return True
        return self._any(cluster, self.clusters) or self._any(f"tg-{cluster}" if cluster else None,
                                                              self.target_groups) or self._any(cluster, self.target_groups)

    def codes_ok(self, rule_codes: list[str]) -> bool:
        return not self.codes or not rule_codes or code_overlap(self.codes, rule_codes)

    def path_ok(self, method: str | None, path_group: str | None) -> bool:
        for m, pat in self.paths:
            if (m is None or (method or "").upper() == m) and path_group is not None and _glob(pat).match(path_group):
                return True
        return False

    def subject_ok(self, rtype: str, subject: str, cluster: str | None) -> bool:
        """A subject-level entry (names only) matches this finding."""
        if self.subjects and not self._any(subject, self.subjects):
            return False
        if self.target_groups or self.clusters:
            if rtype in LB_TYPES:
                if not self.tg_ok(subject):
                    return False
            elif not self.cluster_ok(cluster):
                return False
        return True

    def describe(self) -> str:
        bits = []
        if self.rules:
            bits.append("rules " + ", ".join(self.rules))
        for label, xs in (("target groups", self.target_groups), ("clusters", self.clusters), ("subjects", self.subjects),
                          ("codes", self.codes), ("errors", [f'"{s}"' for s in self.signatures]),
                          ("health", self.health_reasons), ("converter", self.converter_errors)):
            if xs:
                bits.append(f"{label} {', '.join(xs)}")
        if self.paths:
            bits.append("paths " + ", ".join(f"{m + ' ' if m else ''}{p}" for m, p in self.paths))
        return " · ".join(bits)


def _list(v: Any, where: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (str, int)):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, (str, int)) for x in v):
        raise MuteFileError(f"{where}: give a list of names")
    return [str(x).strip() for x in v if str(x).strip()]


def parse(text: str, known_rules: set[str] | None = None) -> list[Mute]:
    """The entries of a mute file (raises MuteFileError with the entry and the problem)."""
    import yaml
    try:
        doc = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        raise MuteFileError(f"Not valid YAML{where}: {getattr(e, 'problem', None) or e}") from None
    doc = doc or {}
    if not isinstance(doc, dict):
        raise MuteFileError("The file must start with 'mutes:'")
    items = doc.get("mutes") or []
    if not isinstance(items, list):
        raise MuteFileError("'mutes' must be a list of entries (each starting with '- id:')")
    out: list[Mute] = []
    seen: set[str] = set()
    for i, e in enumerate(items, 1):
        if not isinstance(e, dict):
            raise MuteFileError(f"Entry {i}: must be a mapping starting with 'id:'")
        mid = str(e.get("id") or "").strip()
        where = f"Entry {i} ({mid or 'no id'})"
        if not ID_RE.match(mid):
            raise MuteFileError(f"{where}: 'id' is required (letters, digits, - _ .; up to 40), e.g. AL-001")
        if mid in seen:
            raise MuteFileError(f"{where}: the id is used twice")
        seen.add(mid)
        extra = set(e) - ENTRY_KEYS
        if extra:
            raise MuteFileError(f"{where}: unknown key(s) {', '.join(sorted(map(str, extra)))} "
                                f"(allowed: {', '.join(sorted(ENTRY_KEYS))})")
        m = e.get("match")
        if not isinstance(m, dict) or not m:
            raise MuteFileError(f"{where}: 'match' is required and must say what to mute")
        extra = set(m) - MATCH_KEYS
        if extra:
            raise MuteFileError(f"{where}: unknown match key(s) {', '.join(sorted(map(str, extra)))} "
                                f"(allowed: {', '.join(sorted(MATCH_KEYS - {'signature'}))})")
        enabled = e.get("enabled", True)
        if not isinstance(enabled, bool):
            raise MuteFileError(f"{where}: 'enabled' must be true or false")
        rules = _list(m.get("rules"), f"{where} rules") or None
        if rules and known_rules is not None:
            bad = [r for r in rules if r not in known_rules]
            if bad:
                raise MuteFileError(f"{where}: unknown rule type(s) {', '.join(bad)} (known: {', '.join(sorted(known_rules))})")
        paths = []
        for p in _list(m.get("paths"), f"{where} paths"):
            method, _, rest = p.partition(" ")
            if rest and method.upper() in METHODS:
                paths.append((method.upper(), rest.strip()))
            elif p.startswith("/"):
                paths.append((None, p))
            else:
                raise MuteFileError(f"{where}: path '{p}' must look like '/path' or 'POST /path'")
        codes = [c.lower() for c in _list(m.get("codes"), f"{where} codes")]
        bad = [c for c in codes if not CODE_RE.match(c)]
        if bad:
            raise MuteFileError(f"{where}: '{bad[0]}' isn't a status code (like 400) or class (like 4xx)")
        sigs = _list(m.get("signatures"), f"{where} signatures") + _list(m.get("signature"), f"{where} signature")
        for s in sigs:
            if '"' in s or len(s) < 4 or len(s) > 300:
                raise MuteFileError(f"{where}: error text '{s[:40]}' must be 4–300 characters without double quotes")
        mute = Mute(id=mid, enabled=enabled, title=str(e.get("title") or ""), owner=str(e.get("owner") or ""),
                    reason=str(e.get("reason") or ""), rules=rules,
                    target_groups=_list(m.get("targetGroups"), f"{where} targetGroups"),
                    clusters=_list(m.get("clusters"), f"{where} clusters"),
                    subjects=_list(m.get("subjects"), f"{where} subjects"),
                    paths=paths, codes=codes, signatures=sigs,
                    health_reasons=_list(m.get("healthReasons"), f"{where} healthReasons"),
                    converter_errors=_list(m.get("converterErrors"), f"{where} converterErrors"),
                    match={k: v for k, v in m.items()})
        kinds = [bool(mute.signatures or mute.paths), bool(mute.health_reasons), bool(mute.converter_errors)]
        if sum(kinds) > 1:
            raise MuteFileError(f"{where}: use paths/signatures, healthReasons or converterErrors in separate entries")
        if not any(kinds) and not (mute.target_groups or mute.clusters or mute.subjects):
            raise MuteFileError(f"{where}: 'match' must name something (targetGroups, clusters, subjects, paths, "
                                f"signatures, healthReasons or converterErrors); 'rules' or 'codes' alone would "
                                f"mute everything")
        out.append(mute)
    return out


class MuteFile:
    """The mute file, read again when it changes. A broken file keeps the last good entries."""

    def __init__(self, path: str, known_rules: set[str] | None = None):
        self.path = path
        self.known_rules = known_rules
        self._lock = threading.Lock()
        self._sig: tuple | None = None
        self.entries: list[Mute] = []
        self.error: str | None = None
        self.loaded_at: float | None = None
        self.exists = False

    def get(self) -> list[Mute]:
        try:
            st = os.stat(self.path)
            sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            sig = None
        except OSError as e:
            with self._lock:
                self.error = f"Can't read {self.path}: {e}"
            return self.entries
        with self._lock:
            if sig == self._sig and (sig is not None or not self.exists):
                return self.entries
            if sig is None:
                self._sig, self.exists, self.entries, self.error = None, False, [], None
                return self.entries
            try:
                with open(self.path, encoding="utf-8") as fh:
                    text = fh.read()
                entries = parse(text, self.known_rules)
            except MuteFileError as e:
                self._sig, self.exists, self.error = sig, True, str(e)
                log.warning("alert mute file %s has a mistake (keeping the last good version): %s", self.path, e)
                return self.entries
            except OSError as e:
                self.error = f"Can't read {self.path}: {e}"
                return self.entries
            import time
            self._sig, self.exists, self.entries, self.error, self.loaded_at = sig, True, entries, None, time.time()
            log.info("alert mute file loaded: %d entries (%d on)", len(entries), sum(1 for m in entries if m.enabled))
            return self.entries
