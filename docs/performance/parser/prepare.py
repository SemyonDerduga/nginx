"""Extract the two header parser functions from the exact upstream and candidate trees."""
from pathlib import Path
import argparse
import hashlib

cli = argparse.ArgumentParser(description=__doc__)
cli.add_argument("baseline", type=Path)
cli.add_argument("candidate", type=Path)
args = cli.parse_args()
where = Path(__file__).resolve().parent
for source, dest in [(args.baseline, "reference.inc"), (args.candidate, "candidate.inc")]:
    blob = (source / "src/http/ngx_http_parse.c").read_bytes()
    if dest == "reference.inc":
        digest = hashlib.sha1(b"blob %d\0" % len(blob) + blob).hexdigest()
        if digest != "e547d0fdb8f450b4817cc8cb24005dd65061b570":
            raise SystemExit("Expected the unmodified NGINX 1.31.8 header parser")
    text = blob.decode()
    start = text.index("ngx_int_t\nngx_http_parse_header_line(")
    end = text.index("\n\n\nngx_int_t\nngx_http_parse_uri(", start)
    body = text[start:end]
    prefix = ""
    helper = "#if (NGX_HAVE_SIMD)\n\n/*\n * returns"
    if helper in body:
        # the SIMD helpers sit between the parser and ngx_http_parse_uri();
        # emit ngx_simd.h (without nginx includes) and the helpers first
        h = body.index(helper)
        body, helper_src = body[:h].rstrip() + "\n", body[h:]
        simd = (source / "src/core/ngx_simd.h").read_text()
        simd = "\n".join(l for l in simd.splitlines()
                         if l not in ("#include <ngx_config.h>", "#include <ngx_core.h>"))
        prefix = simd + "\n\n" + helper_src + "\n\n"
    (where / dest).write_text(prefix + body + "\n")
