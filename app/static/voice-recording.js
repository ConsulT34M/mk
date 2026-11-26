"use strict";

class VoiceRecording {
  constructor(onFailure) {
    if (!window.AudioContext || !window.AudioWorkletNode) {
      throw new Error("이 브라우저는 녹취를 지원하지 않아요. 녹취 선택을 해제하면 음성 상담을 이용할 수 있어요.");
    }
    this.context = new AudioContext({sampleRate: 16000});
    this.onFailure = onFailure;
    this.pending = Promise.resolve();
    this.offset = 0;
    this.sources = [];
  }

  async prepare(processorUrl) {
    await Promise.all([this.context.resume(), this.context.audioWorklet.addModule(processorUrl)]);
    if (this.context.state === "closed") return;
    this.node = new AudioWorkletNode(this.context, "voice-pcm", {channelCount: 1, channelCountMode: "explicit"});
    this.flushed = new Promise(resolve => { this.onFlush = resolve; });
    this.node.port.onmessage = ({data}) => {
      if (data === null) this.onFlush();
      else this.enqueue(data);
    };
    this.node.onprocessorerror = () => this.fail();
  }

  attach(stream) {
    if (!stream) return;
    const source = this.context.createMediaStreamSource(stream);
    source.connect(this.node);
    this.sources.push(source);
  }

  start(url, microphone, remote, startedAt) {
    this.url = url;
    this.startedAt = startedAt;
    this.attach(microphone);
    this.attach(remote);
    this.node.connect(this.context.destination);
  }

  fail() {
    if (!this.failed) this.onFailure();
    this.failed = true;
  }

  enqueue(data) {
    if (this.failed) return;
    const offset = this.offset;
    this.offset += data.byteLength;
    this.pending = this.pending.then(async () => {
      if (this.failed) return;
      const headers = {"Content-Type": "application/octet-stream"};
      if (offset === 0) headers["X-Call-Started-At"] = this.startedAt;
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          const result = await fetch(this.url + "/" + offset, {
            method: "POST", headers,
            body: data, signal: AbortSignal.timeout(10000),
          });
          if (result.ok) return;
          if (result.status < 500) break;
        } catch { /* A lost acknowledgement can be retried at the same byte offset. */ }
      }
      this.fail();
    });
  }

  async stop() {
    if (this.url) {
      this.node.port.postMessage("stop");
      let timer;
      try {
        await Promise.race([this.flushed, new Promise(resolve => {
          timer = setTimeout(() => { this.fail(); resolve(); }, 1500);
        })]);
      } finally {
        clearTimeout(timer);
      }
    }
    this.sources.forEach(source => source.disconnect());
    this.node?.disconnect();
    await this.context.close();
    await this.pending;
  }
}
