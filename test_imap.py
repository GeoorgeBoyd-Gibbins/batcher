from process_batches import send_dispatch_email, SENDER_EMAIL
from pathlib import Path
import tempfile

p = Path(tempfile.mktemp(suffix=".pdf"))
p.write_bytes(b"%PDF-1.0 test")
send_dispatch_email(SENDER_EMAIL, "Test", "XX0000000000GB", "Test", p)
p.unlink()
