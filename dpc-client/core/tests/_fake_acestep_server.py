"""A stand-in for acestep.cpp's `ace-server`, run as a child process by the music engine
tests: the same CLI flags, `/health`, `/props`, `POST /lm`, `POST /synth`, `GET /job`,
`POST /job?cancel=1`, with canned audio codes and a short generated wav24 per variant
(WAVE_FORMAT_EXTENSIBLE, as the real server writes). A multipart `/synth` (parts `request` and
`audio`, as acestep.cpp's `handle_synth` reads them) is a repaint: the source back as wav32 with a
tone per variant inside the window. No GPU.

`FAKE_ACESTEP_MODE` (comma-separated) picks a misbehaviour:
`exit_at_start`, `missing_model`, `crash_on_lm`, `fail_synth`, `slow_synth` (stays running
until cancelled), `short_batch` (one audio part fewer than asked).
`FAKE_ACESTEP_RECORD`, when set, is a folder where each POST body is written as
`<n>-<lm|synth>.json`. SIGTERM (POSIX) or SIGBREAK (Windows) stops it with exit code 0.
"""

import argparse
import asyncio
import json
import math
import os
import secrets
import signal
import struct
import sys

from aiohttp import web

BOUNDARY = "ace-batch-boundary"
MODELS = {
    "lm": ["acestep-5Hz-lm-4B-Q8_0.gguf"],
    "embedding": ["Qwen3-Embedding-0.6B-Q8_0.gguf"],
    "dit": ["acestep-v15-turbo-Q8_0.gguf", "acestep-v15-sft-Q8_0.gguf"],
    "vae": ["vae-BF16.gguf"],
}


def log(msg):
    sys.stderr.write(f"[Fake] {msg}\n")
    sys.stderr.flush()


def wav24(seconds, freq, sample_rate=48000, channels=2):
    n = int(seconds * sample_rate)
    data = bytearray()
    for i in range(n):
        v = int(0.3 * 8388607 * math.sin(2 * math.pi * freq * i / sample_rate))
        sample = (v & 0xFFFFFF).to_bytes(3, "little")
        data += sample * channels
    block = channels * 3
    guid_pcm = struct.pack("<IHH8s", 1, 0x0000, 0x0010, b"\x80\x00\x00\xaa\x00\x38\x9b\x71")
    fmt = struct.pack("<HHIIHHHHI", 0xFFFE, channels, sample_rate, sample_rate * block, block, 24, 22, 24,
                      3) + guid_pcm
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + bytes(data)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--models", required=True)
    ap.add_argument("--max-batch", type=int, default=1)
    ap.add_argument("--device")
    args = ap.parse_args()
    modes = set(filter(None, os.environ.get("FAKE_ACESTEP_MODE", "").split(",")))
    record = os.environ.get("FAKE_ACESTEP_RECORD")
    log(f"acestep.cpp fake listening on {args.host}:{args.port} max_batch={args.max_batch} modes={sorted(modes)}")
    if "exit_at_start" in modes:
        log("exit_at_start")
        sys.exit(3)

    jobs = {}
    counter = {"n": 0}

    def recorded(kind, body):
        if record:
            counter["n"] += 1
            os.makedirs(record, exist_ok=True)
            with open(os.path.join(record, f"{counter['n']:03d}-{kind}.json"), "wb") as f:
                f.write(body)

    async def health(_req):
        return web.json_response({"status": "ok"})

    async def props(_req):
        models = {k: list(v) for k, v in MODELS.items()}
        if "missing_model" in modes:
            models["dit"] = ["acestep-v15-sft-Q8_0.gguf"]
        return web.json_response({"version": "fake0000 (2026-11-21)", "models": models,
                                  "cli": {"max_batch": args.max_batch}})

    def new_job(status="running"):
        jid = secrets.token_hex(8)
        jobs[jid] = {"status": status, "body": b"", "mime": "application/json"}
        return jid

    async def lm(req):
        body = await req.read()
        recorded("lm", body)
        if "crash_on_lm" in modes:
            log("crash_on_lm")
            os._exit(7)
        r = json.loads(body)
        codes = ",".join(str(1000 + i) for i in range(int(float(r.get("duration", 10)) * 5)))
        out = dict(r)
        out.update({"caption": "an LM caption: " + r.get("caption", ""), "audio_codes": codes, "keyscale": "E minor",
                    "timesignature": "4", "lm_seed": 876362762, "solver": "euler", "lm_mode": "generate",
                    "task_type": "text2music"})
        jid = new_job()
        jobs[jid].update(status="done", body=json.dumps([out], ensure_ascii=False).encode("utf-8"))
        return web.json_response({"id": jid})

    async def repaint(req):
        """`POST /synth` multipart (part `request`, part `audio`) as acestep.cpp's handle_synth reads it:
        task_type repaint answers each variant as the source with a tone of its own inside the window
        [int(start*25), int(end*25)) latent frames, on the 25 Hz grid, as wav32 float, so a test can tell
        what came from where. Without `audio` or a caption it fails, as the real server refuses."""
        import numpy as np
        reader = await req.multipart()
        request_body, audio = None, None
        async for part in reader:
            data = await part.read(decode=False)
            if part.name == "request":
                request_body = bytes(data)
            elif part.name == "audio":
                audio = bytes(data)
        recorded("synth", request_body or b"")
        r = json.loads(request_body)
        jid = new_job()
        if r.get("task_type") != "repaint" or audio is None or not r.get("caption") or "fail_synth" in modes:
            jobs[jid]["status"] = "failed"
            return web.json_response({"id": jid})
        if "slow_synth" in modes:
            return web.json_response({"id": jid})
        pos, fmt, data_off, data_len = 12, None, None, None
        while pos + 8 <= len(audio):
            cid, size = audio[pos:pos + 4], struct.unpack("<I", audio[pos + 4:pos + 8])[0]
            if cid == b"fmt ":
                tag, ch, rate, _br, _ba, bits = struct.unpack("<HHIIHH", audio[pos + 8:pos + 24])
                fmt = (tag, ch, rate, bits)
            elif cid == b"data":
                data_off, data_len = pos + 8, min(size, len(audio) - pos - 8)
                break
            pos += 8 + size + (size % 2)
        _tag, ch, rate, bits = fmt
        raw = audio[data_off:data_off + data_len]
        if bits == 24:
            b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            ints = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            src = (np.where(ints & 0x800000, ints - 0x1000000, ints) / 8388608.0).astype(np.float32)
        elif bits == 16:
            src = (np.frombuffer(raw, dtype="<i2") / 32768.0).astype(np.float32)
        else:
            src = np.frombuffer(raw, dtype="<f4").astype(np.float32)
        src = src.reshape(-1, ch)
        n_out = int(math.ceil(src.shape[0] / 1920)) * 1920
        t0 = int(float(r.get("repainting_start", 0)) * 25) * 1920
        t1 = min(n_out, int(float(r.get("repainting_end", -1)) * 25) * 1920)
        n = int(r.get("synth_batch_size", 1)) - (1 if "short_batch" in modes else 0)
        seed = int(r.get("seed", 0))
        parts = b""
        for i in range(n):
            out = np.zeros((n_out, ch), dtype=np.float32)
            out[:src.shape[0]] = src
            k = np.arange(t0, t1)
            out[t0:t1] = (0.25 * np.sin(2 * np.pi * (440 + 20 * ((seed + i) % 10)) * k / rate))[:, None]
            pcm = out.reshape(-1).astype("<f4").tobytes()
            fmt_chunk = struct.pack("<HHIIHH", 3, ch, rate, rate * ch * 4, ch * 4, 32)
            body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk + b"data" + \
                struct.pack("<I", len(pcm)) + pcm
            wav = b"RIFF" + struct.pack("<I", len(body)) + body
            parts += (f"--{BOUNDARY}\r\nContent-Type: audio/wav\r\n\r\n").encode() + wav + b"\r\n"
            latent = struct.pack("<64f", *([float(seed + i)] * 64))
            parts += (f"--{BOUNDARY}\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + latent + b"\r\n"
        parts += f"--{BOUNDARY}--\r\n".encode()
        jobs[jid].update(status="done", body=parts, mime=f"multipart/mixed; boundary={BOUNDARY}")
        return web.json_response({"id": jid})

    async def synth(req):
        if req.content_type.startswith("multipart/"):
            return await repaint(req)
        body = await req.read()
        recorded("synth", body)
        r = json.loads(body)
        jid = new_job()
        if "fail_synth" in modes:
            jobs[jid]["status"] = "failed"
            log("synth failed on purpose")
            return web.json_response({"id": jid})
        if "slow_synth" in modes:
            return web.json_response({"id": jid})
        n = int(r.get("synth_batch_size", 1))
        if "short_batch" in modes:
            n -= 1
        seed = int(r.get("seed", 0))
        parts = b""
        for i in range(n):
            audio = wav24(0.05, 220 + 20 * ((seed + i) % 10))
            parts += (f"--{BOUNDARY}\r\nContent-Type: audio/wav\r\n\r\n").encode() + audio + b"\r\n"
            latent = struct.pack("<64f", *([float(seed + i)] * 64))
            parts += (f"--{BOUNDARY}\r\nContent-Type: application/octet-stream\r\n"
                      f"Content-Disposition: form-data; name=\"latent\"\r\n\r\n").encode() + latent + b"\r\n"
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
        job = jobs.get(req.query.get("id", ""))
        if job is None:
            return web.json_response({"error": "Job not found"}, status=404)
        if req.query.get("cancel") == "1":
            if job["status"] == "running":
                job["status"] = "cancelled"
                log(f"cancel requested for job {req.query.get('id')}")
            return web.json_response({"status": job["status"]})
        return web.json_response({"error": "Unknown action"}, status=400)

    async def run():
        app = web.Application(client_max_size=256 * 1024 * 1024)
        app.add_routes([web.get("/health", health), web.get("/props", props), web.post("/lm", lm),
                        web.post("/synth", synth), web.get("/job", job_get), web.post("/job", job_post)])
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
