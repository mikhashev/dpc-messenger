"""A stand-in for yue2.cpp's `yue-server`, run as a child process by the music engine tests:
the same CLI flags, `/health`, `/props`, `POST /synth`, `GET /job`, `POST /job?cancel=1`,
the server's own request checks (a 400 for `lm_batch_size` over `--max-batch`, a
`synth_batch_size` outside 1-9, an adapter not in `--adapters`), and a multipart result per
track (the replay request as JSON, a short wav24, the latents). With `--transcriber`, `POST
/transcribe` (multipart `audio`, optional field `melody_only`) answers a canned ABC score, as SheetSage2
would; the replay request echoes a sent `abc`. No GPU.

`FAKE_YUE2_MODE` (comma-separated) picks a misbehaviour: `exit_at_start`, `wrong_model`
(`/props` names another backbone), `fail_synth`, `slow_synth` (stays running until
cancelled), `skip_instrumental` (an adapter whose name holds "instrumental" is left out, as
the real server leaves out one it cannot merge), `native_abc` (`/transcribe` answers a score in
SheetSage2's native two-voice dialect with named sections, `NATIVE_ABC`), `follow_length` (a track is
as long as its semantic `min_tokens` floor at 25 frames a second, the length a cover asks for), `big_abc`
(`/transcribe` answers a score of about 40 kB, more than the context holds). `FAKE_YUE2_RECORD`, when set, is a folder where each `/synth` body is written as
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
# 4/4 at 120 BPM: a bar is 2 s. An intro bar with no voice, two verse bars, two chorus bars.
NATIVE_ABC = "\n".join([
    "X:1", "T:", "M:4/4", "L:1/16", "Q:1/4=120",
    'V: Vocal clef=treble name="Vocal Melody" snm="Vocal"', 'V: Ins clef=treble name="Ins Melody" snm="Inst."',
    "K:C", "% intro", "V: Vocal", 'Z|', "V: Ins", "c4d4e4f4|",
    "% verse", "V: Vocal", '"C"C4D4E4F4|"G"G4A4B4c4|', "V: Ins", "Z2|",
    "% chorus", "V: Vocal", '"F"c4B4A4G4|"C"F4E4D4C4|', "V: Ins", "e4d4c4B4|Z|", ""])


def log(msg):
    sys.stderr.write(f"[Fake yue2] {msg}\n")
    sys.stderr.flush()


def scan(folder, skip=""):
    """The adapters `--adapters` offers, as yue2.cpp's scan reads a folder."""
    out = []
    if not folder or not os.path.isdir(folder):
        return out
    for name in sorted(n for n in os.listdir(folder) if not n.startswith(".")):
        path = os.path.join(folder, name)
        if skip and skip in name:
            log(f"[Adapter] Skip {name}: unsupported tensor")
            continue
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
    ap.add_argument("--transcriber")
    args = ap.parse_args()
    modes = set(filter(None, os.environ.get("FAKE_YUE2_MODE", "").split(",")))
    record = os.environ.get("FAKE_YUE2_RECORD")
    log(f"yue2.cpp fake on {args.host}:{args.port} model={os.path.basename(args.model)} "
        f"max_batch={args.max_batch} modes={sorted(modes)}")
    if "exit_at_start" in modes:
        log("exit_at_start")
        sys.exit(3)
    adapters = scan(args.adapters, "instrumental" if "skip_instrumental" in modes else "")
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
        sem = dict({"min_tokens": 200, "max_tokens": 9000}, **(r.get("semantic_sampling") or {}))
        if not 0 <= int(sem["min_tokens"]) <= int(sem["max_tokens"]):
            return bad("semantic preset outside the protocol bounds")
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
                          abc=r.get("abc") or "X:1\nK:Em\n|E2 G2|", semantic_tokens=",".join(str(100 + k) for k in range(25)))
            parts += (f"--{BOUNDARY}\r\nContent-Type: application/json\r\n\r\n".encode()
                      + json.dumps(replay, ensure_ascii=False).encode("utf-8") + b"\r\n")
            seconds = 0.05
            if "follow_length" in modes and (r.get("semantic_sampling") or {}).get("min_tokens"):
                seconds = int(r["semantic_sampling"]["min_tokens"]) / 25
            audio = wav24(seconds, 300 + 15 * ((seed + i) % 10))
            parts += f"--{BOUNDARY}\r\nContent-Type: audio/wav\r\n\r\n".encode() + audio + b"\r\n"
            latent = struct.pack("<64f", *([float(seed + i)] * 64))
            parts += (f"--{BOUNDARY}\r\nContent-Type: application/octet-stream\r\n\r\n".encode() + latent + b"\r\n")
        parts += f"--{BOUNDARY}--\r\n".encode()
        jobs[jid].update(status="done", body=parts, mime=f"multipart/mixed; boundary={BOUNDARY}")
        return web.json_response({"id": jid})

    async def transcribe(req):
        reader = await req.multipart()
        audio, melody_only = None, False
        async for part in reader:
            data = await part.read(decode=False)
            if part.name == "audio":
                audio = bytes(data)
            elif part.name == "melody_only":
                melody_only = True
        recorded("transcribe.json", json.dumps({"melody_only": melody_only,
                                                "audio_bytes": len(audio or b"")}).encode("utf-8"))
        if audio is None:
            return bad("multipart audio part required")
        jid = secrets.token_hex(8)
        abc = "X:1\nT:fake transcription\nK:C\n" + ("|C D E F|" if melody_only else '|"C"C D "G"E F|')
        if "native_abc" in modes:
            abc = NATIVE_ABC
        if "big_abc" in modes:
            abc += "|C D E F|" * 4000     # a score of about 40 kB, more than a context holds
        jobs[jid] = {"status": "done", "body": json.dumps({"abc": abc}).encode("utf-8"), "mime": "application/json"}
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
        if args.transcriber:
            app.add_routes([web.post("/transcribe", transcribe)])
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
