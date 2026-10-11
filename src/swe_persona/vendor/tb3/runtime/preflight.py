"""Report infrastructure prerequisites before creating any task trial."""
import datetime
import os
import socket
import subprocess
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def check(require_docker=True):
    report = {"checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
              "ready": False, "model_calls": 0, "checks": []}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM):
            pass
    except OSError as exc:
        report.update(blocker="network_permission_denied" if exc.errno in (1, 13) else "network_unavailable",
                      reason=f"{type(exc).__name__}: {exc}")
        return report
    endpoint = os.environ.get("AZURE_OPENAI_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    key_available = bool(os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY"))
    if not endpoint or not key_available:
        report.update(blocker="missing_host_api_configuration", reason="Set host base URL and API key.")
        return report
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        report.update(blocker="invalid_endpoint", reason="A credential-free HTTPS API base URL is required.")
        return report
    try:
        socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        try:
            with urllib.request.urlopen(endpoint, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code  # 401/404 still establishes DNS/TLS reachability.
        report["checks"].append({"check": "api_dns_tls", "http_status": status})
    except Exception as exc:
        report.update(blocker="api_unreachable", reason=type(exc).__name__)
        return report
    if require_docker:
        try:
            result = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                                    timeout=15, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError("Docker daemon unavailable")
            report["checks"].append({"check": "docker_daemon", "ready": True})
        except Exception as exc:
            report.update(blocker="docker_unavailable", reason=str(exc)[-500:])
            return report
    report.update(ready=True, blocker=None)
    return report
