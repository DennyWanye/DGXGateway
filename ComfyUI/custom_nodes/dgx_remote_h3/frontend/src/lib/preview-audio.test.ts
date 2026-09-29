// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { PreviewAudioMixer } from './preview-audio';

function fixture() {
  const source = { connect: vi.fn(), disconnect: vi.fn() };
  const gain = { gain: { value: 0 }, connect: vi.fn(), disconnect: vi.fn() };
  const context = { resume: vi.fn().mockResolvedValue(undefined), close: vi.fn().mockResolvedValue(undefined),
    createMediaElementSource: vi.fn().mockReturnValue(source), createGain: vi.fn().mockReturnValue(gain),
    destination: {}, state: 'running' };
  const mixer = new PreviewAudioMixer(() => context as unknown as AudioContext);
  const element = document.createElement('audio'); element.pause = vi.fn();
  return { mixer, context, source, gain, element };
}
describe('timeline audio preview', () => {
  it('preserves positive gain instead of capping at the HTML volume limit', async () => {
    const f = fixture(); await f.mixer.resume();
    const amplitude = 10 ** (6 / 20);
    f.mixer.setGain(f.element, amplitude);
    expect(f.element.volume).toBe(1);
    expect(f.gain.gain.value).toBeCloseTo(1.995262);
    f.mixer.setGain(f.element, amplitude / 2);
    expect(f.gain.gain.value).toBeCloseTo(.997631);
  });
  it('reuses a media source through frames and reconnects without creating it twice', async () => {
    const f = fixture(); await f.mixer.resume();
    f.mixer.setGain(f.element, .5); f.mixer.setGain(f.element, .2);
    f.mixer.retain([]); f.mixer.setGain(f.element, 1);
    expect(f.context.createMediaElementSource).toHaveBeenCalledTimes(1);
    expect(f.source.disconnect).toHaveBeenCalledOnce();
    expect(f.source.connect).toHaveBeenCalledTimes(2);
  });
  it('disconnects removed sources and closes the context when leaving the preview', async () => {
    const f = fixture(); await f.mixer.resume(); f.mixer.setGain(f.element, 1);
    f.mixer.retain([f.element]); expect(f.source.disconnect).not.toHaveBeenCalled();
    await f.mixer.close(); await f.mixer.close();
    expect(f.source.disconnect).toHaveBeenCalledOnce();
    expect(f.gain.disconnect).toHaveBeenCalledOnce();
    expect(f.context.close).toHaveBeenCalledOnce();
    expect(f.element.pause).toHaveBeenCalledOnce();
  });
  it('coalesces pending play calls and treats boundary cancellation as normal', async () => {
    const f = fixture(); let reject!: (error: unknown) => void;
    f.element.play = vi.fn(() => new Promise<void>((_resolve, fail) => { reject = fail; }));
    const failure = vi.fn(); f.mixer.play(f.element, failure); f.mixer.play(f.element, failure);
    expect(f.element.play).toHaveBeenCalledOnce();
    reject(new DOMException('Paused at the end of the audio clip', 'AbortError'));
    await Promise.resolve(); await Promise.resolve();
    expect(failure).not.toHaveBeenCalled();
    f.element.play = vi.fn().mockRejectedValue(new DOMException('Cannot decode', 'NotSupportedError'));
    f.mixer.play(f.element, failure); await Promise.resolve(); await Promise.resolve();
    expect(failure).toHaveBeenCalledOnce();
  });
});
