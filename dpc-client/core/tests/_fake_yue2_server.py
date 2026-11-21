"""A stand-in for yue2.cpp's `yue-server`, run as a child process by the music engine tests:
the same CLI flags, `/health`, `/props`, `POST /synth`, `GET /job`, `POST /job?cancel=1`,
the server's own request checks (a 400 for `lm_batch_size` over `--max-batch`, a
`synth_batch_size` outside 1-9, an adapter not in `--adapters`), and a multipart result per
track (the replay request as JSON, a short wav24, the latents). No GPU.

`FAKE_YUE2_MODE` (comma-separated) picks a misbehaviour: `exit_at_start`, `wrong_model`
(`/props` names another backbone), `fail_synth`, `slow_synth` (stays running until
cancelled). `FAKE_YUE2_RECORD`, when set, is a folder where each `/synth` body is written as
`<n>-synth.json` and each cancel as `<n>-cancel.txt`. SIGTERM (POSIX) or SIGBREAK (Windows)
stops it with exit code 0.
"""

import argparse
import asyncio
import json
import os
import secrets
import signal
import struct
import sys

from aiohttp import web

from _fake_acestep_server import wav24

BOUNDARY = "yue2-batch-boundary"


def log(msg):
    sys.stderr.write(f"[Fake yue2] {msg}\n")
    sys.stderr.flush()


def scan(folder):
    """The adapters `--adapters` offers, as yue2.cpp's scan reads a folder."""
    out = []
    if not folder or not os.path.isdir(folder):
        return out
    for name in sorted(n for n in os.listdir(folder) if not n.startswith(".")):
        path = os.path.join(folder, name)
        if os.path.isdir(path):
            if not any(f.endswith(".safetensors") for f in os.listdir(path)):
                continue
        elif not name.endswith(".safetensors"):
            continue
        ar, nar = not name.endswith("-sound.safetensors"), not name.endswith("-music.safetensors")
        out.append({"name": name, "ar": ar, "nar": nar})
        log(f"[Adapter] {name}:{' AR' if ar else ''}{' NAR' if nar else ''}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--vae", required=True)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8087)
    ap.add_argument("--max-batch", type=int, default=1)
    ap.add_argument("--adapters")
    ap.add_argument("--device")
    ap.add_argument("--max-seq", type=int)
    args = ap.parse_args()
    modes = set(filter(None, os.environ.get("FAKE_YUE2_MODE", "").split(",")))
    record = os.environ.get("FAKE_YUE2_RECORD")
    log(f"yue2.cpp fake on {args.host}:{args.port} model={os.path.basename(args.model)} "
        f"max_batch={args.max_batch} modes={sorted(modes)}")
    if "exit_at_start" in modes:
        log("exit_at_start")
        sys.exit(3)
    adapters = scan(args.adapters)
    jobs = {}
    counter = {"n": 0}

    def recorded(kind, body):
        if record:
            counter["n"] += 1
            os.makedirs(record, exist_ok=True)
            with open(os.path.join(record, f"{counter['n']:03d}-{kind}"), "wb") as f:
                f.write(body)

    async def health(_req):
        return web.json_response({"status": "ok"})

    async def props(_req):
        model = "models/other-backbone.gguf" if "wrong_model" in modes else args.model
        return web.json_response({"version": "e646d52 (2026-11-17)", "model": model, "vae": args.vae,
                                  "sample_rate": 48000, "frame_rate": 25, "context": 24576,
                                  "adapters": adapters, "defaults": {"cot": "full", "steps": 32}})

    def bad(message):
        return web.json_response({"error": message}, status=400)

    async def synth(req):
        body = await req.read()
        recorded("synth.json", body)
        try:
            r = json.loads(body)
        except ValueError:
            return bad("invalid JSON")
        if r.get("cot", "full") not in ("full", "melody", "off"):
            return bad("cot must be full, melody or off")
        if not 1 <= int(r.get("lm_batch_size", 1)) <= args.max_batch:
            return bad("lm_batch_size exceeds --max-batch")
        if not 1 <= int(r.get("synth_batch_size", 1)) <= 9:
            return bad("synth_batch_size must be between 1 and 9")
        names = {a["name"] for a in adapters}
        for a in r.get("adapters") or []:
            if a.get("name") not in names:
                return bad("adapter not found: " + str(a.get("name")))
        jid = secrets.token_hex(8)
        jobs[jid] = {"status": "running", "body": b"", "mime": "application/json"}
        if "fail_synth" in modes:
            jobs[jid]["status"] = "failed"
            log("synth failed on purpose")
            return web.json_response({"id": jid})
        if "slow_synth" in modes:
            return web.json_response({"id": jid})
        seed = int(r.get("seed", 7))
        lm_seed = int(r.get("lm_seed", seed))
        parts = b""
        n = int(r.get("lm_batch_size", 1)) * int(r.get("synth_batch_size", 1))
        for i in range(n):
            replay = dict(r, seed=seed + i, lm_seed=lm_seed, lm_batch_size=1, synth_batch_size=1,
                          abc="X:1\nK:Em\n|E2 G2|", semantic_tokens=",".join(str(100 + k) for k in range(25)))
            parts += (f"--{BOUNDARY}\r\nContent-Type: application/json\r\n\r\n".encode()
                      + json.dumps(replay, ensure_ascii=False).encode("utf-8") + b"\r\n")
            audio = wav24(0.05, 300 + 15 * ((seed + i) % 10))
            parts += f"--{BOUNDARY}\r\nContent-Type: audio/wav\r\n\r\n".encode() + audio + b"\r\n"
            latent = struct.pack("<64f", *([float(seed + i)] * 64))
            parts += (f"--{BOUNDARY}\r\nContent-Type: application/octet-stream\r\n\r\n".encode() + latent + b"\r\n")
        parts += f"--{BOUNDARY}--\r\n".encode()
        jobs[jid].update(status="done", body=parts, mime=f"multipart/mixed; boundary={BOUNDARY}")
        return web.json_response({"id": jid})

    async def job_get(req):
        job = jobs.get(req.query.get("id", ""))
        if job is None:
            return web.json_response({"error": "Job not found"}, status=404)
        if req.query.get("result") == "1":
            if job["status"] != "done":
                return web.json_response({"error": "Result not ready"}, status=404)
            return web.Response(body=job["body"], headers={"Content-Type": job["mime"]})
        return web.json_response({"status": job["status"]})

    async def job_post(req):
        jid = req.query.get("id", "")
        job = jobs.get(jid)
        if job is None:
            return web.json_response({"error": "Job not found"}, status=404)
        if req.query.get("cancel") == "1":
            recorded("cancel.txt", jid.encode())
            if job["status"] == "running":
                job["status"] = "cancelled"
                log(f"cancel requested for job {jid}")
            return web.json_response({"status": job["status"]})
        return web.json_response({"error": "Unknown action"}, status=400)

    async def run():
        app = web.Application(client_max_size=256 * 1024 * 1024)
        app.add_routes([web.get("/health", health), web.get("/props", props), web.post("/synth", synth),
                        web.get("/job", job_get), web.post("/job", job_post)])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, args.host, args.port)
        await site.start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_signal(*_):
            loop.call_soon_threadsafe(stop.set)
        for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), on_signal)
        await stop.wait()
        log("graceful stop")
        await runner.cleanup()

    asyncio.run(run())
    sys.exit(0)


if __name__ == "__main__":
    main()
