class VoicePCM extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = new Int16Array(sampleRate);
    this.used = 0;
    this.stopped = false;
    this.port.onmessage = () => {
      this.stopped = true;
      if (this.used) this.port.postMessage(this.samples.slice(0, this.used).buffer);
      this.port.postMessage(null);
    };
  }

  process(inputs) {
    if (this.stopped) return false;
    for (const value of inputs[0][0] || []) {
      const sample = Math.max(-1, Math.min(1, value));
      this.samples[this.used++] = Math.round(sample * (sample < 0 ? 32768 : 32767));
      if (this.used === this.samples.length) {
        this.port.postMessage(this.samples.buffer, [this.samples.buffer]);
        this.samples = new Int16Array(sampleRate);
        this.used = 0;
      }
    }
    // Leave the output silent. Only the remote audio element plays over the speakers.
    return true;
  }
}

registerProcessor("voice-pcm", VoicePCM);
