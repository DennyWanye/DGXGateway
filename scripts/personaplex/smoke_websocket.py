#!/usr/bin/env python3
"""Exercise an already Gateway-started PersonaPlex; never loads/stops a model."""
import argparse
import asyncio
import json
from pathlib import Path
import time
from urllib.parse import urlencode

import aiohttp
import numpy as np
import sphn


async def run(args):
    audio, rate = sphn.read(args.input)
    if rate != 24000:
        raise ValueError('fixture must be 24 kHz; resample before smoke')
    pcm = np.asarray(audio, dtype=np.float32).mean(axis=0)
    pcm = pcm[:int(args.seconds * rate)]
    if not len(pcm):
        raise ValueError('empty audio fixture')
    # Append silence to allow the model to complete its reply.
    pcm = np.concatenate([pcm, np.zeros(int(args.tail_seconds * rate), dtype=np.float32)])
    encoder = sphn.OpusStreamWriter(rate)
    decoder = sphn.OpusStreamReader(rate)
    chunks, text = [], []
    started = time.monotonic()
    first_audio = None
    url = args.url.rstrip('/') + '/api/chat?' + urlencode({
        'voice_prompt': args.voice, 'text_prompt': args.prompt, 'seed': str(args.seed)})
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        async with client.get(args.url.rstrip('/') + '/healthz') as response:
            response.raise_for_status()
            health = await response.json()
            if not health.get('ready') or health.get('draining'):
                raise RuntimeError(f'runtime not admitting: {health}')
        async with client.ws_connect(url) as ws:
            async def receive():
                nonlocal first_audio
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        data = message.data
                        if data[:1] == b'\x01':
                            decoder.append_bytes(data[1:])
                            frame = decoder.read_pcm()
                            if frame.size:
                                if first_audio is None:
                                    first_audio = time.monotonic() - started
                                chunks.append(np.array(frame, copy=True).reshape(-1))
                        elif data[:1] == b'\x02':
                            text.append(data[1:].decode('utf-8'))
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise RuntimeError(str(ws.exception()))

            handshake = await asyncio.wait_for(ws.receive(), timeout=90)
            if handshake.type != aiohttp.WSMsgType.BINARY or handshake.data != b'\x00':
                raise RuntimeError(f'invalid handshake: {handshake.type}')
            receiver = asyncio.create_task(receive())
            try:
                # Concurrent admission must reject before changing active voice state.
                try:
                    other = await client.ws_connect(url)
                except aiohttp.WSServerHandshakeError as error:
                    if error.status != 409:
                        raise
                else:
                    await other.close()
                    raise RuntimeError('second session was incorrectly admitted')
                if args.check_drain:
                    async with client.post(args.url.rstrip('/') + '/drain') as response:
                        response.raise_for_status()
                        status = await response.json()
                        if not status.get('draining') or status.get('active_sessions') != 1:
                            raise RuntimeError(f'bad active drain state: {status}')
                    try:
                        other = await client.ws_connect(url)
                    except aiohttp.WSServerHandshakeError as error:
                        if error.status != 503:
                            raise
                    else:
                        await other.close()
                        raise RuntimeError('draining service admitted a new session')
                send_started = time.monotonic()
                for offset in range(0, len(pcm), 1920):
                    encoder.append_pcm(pcm[offset:offset + 1920])
                    packet = encoder.read_bytes()
                    if packet:
                        await ws.send_bytes(b'\x01' + packet)
                    delay = (offset + 1920) / rate - (time.monotonic() - send_started)
                    if delay > 0:
                        await asyncio.sleep(delay)
                await asyncio.sleep(args.receive_tail)
                await ws.close()
                await receiver
            finally:
                if not receiver.done():
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)
        if args.check_drain:
            for _ in range(30):
                async with client.get(args.url.rstrip('/') + '/healthz') as response:
                    final_health = await response.json()
                if final_health.get('active_sessions') == 0:
                    break
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError('closed session remains active')
    output = np.concatenate(chunks) if chunks else np.empty(0, dtype=np.float32)
    if not len(output) or not np.isfinite(output).all() or float(np.max(np.abs(output))) < 1e-5:
        raise RuntimeError('no valid nonzero audio returned')
    transcript = ''.join(text).strip()
    if not transcript:
        raise RuntimeError('no text returned; inspect audio and retry a longer conversational fixture')
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sphn.write_wav(str(destination), output, rate)
    report = {'endpoint': args.url, 'voice': args.voice, 'seed': args.seed,
              'output_wav': str(destination.resolve()), 'audio_seconds': len(output) / rate,
              'first_audio_seconds': first_audio, 'elapsed_seconds': time.monotonic() - started,
              'text': transcript, 'peak': float(np.max(np.abs(output))),
              'concurrent_rejected': True, 'drain_checked': args.check_drain,
              'quality_status': 'protocol passed; human listening required for intelligibility'}
    destination.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://192.0.2.30:8998')
    parser.add_argument('--input', default='/home/operator/dgx-models/personaplex/assets/test/input_assistant.wav')
    parser.add_argument('--output', required=True)
    parser.add_argument('--voice', default='NATF0.pt')
    parser.add_argument('--prompt', default='You are a wise and friendly teacher. Answer questions or provide advice in a clear and engaging way.')
    parser.add_argument('--seed', type=int, default=42424242)
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--tail-seconds', type=float, default=8)
    parser.add_argument('--receive-tail', type=float, default=5)
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--check-drain', action='store_true', help='Leaves service draining; next action must be Gateway stop/switch.')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 120 or not 0 <= args.tail_seconds <= 30 or not 0 <= args.receive_tail <= 30:
        parser.error('audio/tail duration outside allowed bounds')
    asyncio.run(asyncio.wait_for(run(args), timeout=args.timeout))


if __name__ == '__main__':
    main()
