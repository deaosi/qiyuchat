"""Run the bounded export projection separately from the Feishu CLI writer."""

import json
from pathlib import Path
import subprocess
import sys


MAX_RESPONSE_BYTES = 16_000_000
_SNAPSHOT = Path(__file__).with_name("snapshot.py")


def fetch(last: dict, apply: bool = False, *, probe: bool = False, marker: str = "", job_only: bool = False) -> dict:
    """Return the new publication snapshot. Preview never issues guest links."""
    links = {"topics": {}, "sops": {}}
    if not probe and not job_only:
        for document in last.get("published_sops", []):
            for record in document["records"]:
                links["sops"][record["key"]] = record["url"]
        for kind, index in links.items():
            for record in last.get("documents", {}).get(kind, {}).get("records", []):
                index[record["key"]] = record["url"]
    request = json.dumps({"apply": apply, "links": links, "probe": probe, "marker": marker, "job_only": job_only},
                         ensure_ascii=True)
    raw_request = request.encode("utf-8")
    if len(raw_request) > MAX_RESPONSE_BYTES:
        raise ValueError("生产侧发布快照请求超出大小限制。")
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-u", str(_SNAPSHOT)], input=raw_request,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=180, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("无法读取当前语海Voc快照。") from None
    if result.returncode or len(result.stdout) > MAX_RESPONSE_BYTES:
        raise ValueError("生产侧未能生成完整的高频问题发布快照。")
    snapshot = json.loads(result.stdout)
    if not isinstance(snapshot, dict):
        raise ValueError("生产侧返回了无效的发布快照。")
    return snapshot
