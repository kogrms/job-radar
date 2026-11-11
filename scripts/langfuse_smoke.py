"""Send one test trace to Langfuse and print its id. Usage: uv run python scripts/langfuse_smoke.py"""

import sys

from langfuse import Langfuse

from job_radar.config import get_settings


def main() -> int:
    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        print("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not set", file=sys.stderr)
        return 1
    lf = Langfuse(
        public_key=s.langfuse_public_key,
        secret_key=s.langfuse_secret_key.get_secret_value(),
        base_url=s.langfuse_base_url,
    )
    if not lf.auth_check():
        print(f"auth_check failed against {s.langfuse_base_url}", file=sys.stderr)
        return 1
    with lf.start_as_current_observation(name="smoke-test", input={"hello": "langfuse"}) as span:
        span.update(output={"ok": True})
        trace_id = lf.get_current_trace_id()
    lf.flush()
    print(f"trace sent: {s.langfuse_base_url}/trace/{trace_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
