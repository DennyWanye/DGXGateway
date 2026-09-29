/** One Web Audio context per timeline preview, owned until that preview closes. */
export class PreviewAudioMixer {
  private context: AudioContext | null = null;
  private known = new WeakMap<HTMLMediaElement, { source: MediaElementAudioSourceNode; gain: GainNode }>();
  private connected = new Map<HTMLMediaElement, { source: MediaElementAudioSourceNode; gain: GainNode }>();
  private starting = new WeakSet<HTMLMediaElement>();

  constructor(private createContext = () => new AudioContext()) {}

  async resume() {
    this.context ??= this.createContext();
    await this.context.resume();
  }

  setGain(element: HTMLMediaElement, value: number) {
    if (!this.context) return;
    let nodes = this.known.get(element);
    if (!nodes) {
      nodes = { source: this.context.createMediaElementSource(element), gain: this.context.createGain() };
      this.known.set(element, nodes);
    }
    if (!this.connected.has(element)) {
      nodes.source.connect(nodes.gain);
      nodes.gain.connect(this.context.destination);
      this.connected.set(element, nodes);
    }
    element.volume = 1;
    nodes.gain.gain.value = Math.max(0, value);
  }

  play(element: HTMLMediaElement, onFailure: (error: unknown) => void) {
    if (!element.paused || this.starting.has(element)) return;
    this.starting.add(element);
    void element.play().catch(error => {
      // Pausing at a clip boundary can abort a pending play promise normally.
      if (error?.name !== 'AbortError') onFailure(error);
    }).finally(() => this.starting.delete(element));
  }

  retain(elements: Iterable<HTMLMediaElement>) {
    const keep = new Set(elements);
    for (const [element, nodes] of this.connected) {
      if (!keep.has(element)) {
        element.pause();
        nodes.source.disconnect();
        nodes.gain.disconnect();
        this.connected.delete(element);
      }
    }
  }

  async close() {
    this.retain([]);
    const context = this.context;
    this.context = null;
    this.known = new WeakMap();
    if (context && context.state !== 'closed') await context.close();
  }
}
