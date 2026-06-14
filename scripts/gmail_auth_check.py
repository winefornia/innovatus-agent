"""Check Gmail auth mode and basic mailbox access without sending email."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    from services.gmail_service import get_auth_status, list_labels

    status = get_auth_status()
    labels = list_labels()
    print(json.dumps({
        "auth": status,
        "label_count": len(labels),
        "sample_labels": [label.get("name") for label in labels[:10]],
    }, indent=2))


if __name__ == "__main__":
    main()
