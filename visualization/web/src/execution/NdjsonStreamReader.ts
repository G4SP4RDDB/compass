// Reads a fetch Response body as newline-delimited JSON, calling onEvent(obj)
// for each line as it arrives — NOT after the whole response is buffered,
// the whole point being to show progress while a live hop is still running
// (see POST /api/test-hop's streamed branch in server.py). Tolerates a
// final line with no trailing "\n" (whatever's left in `buf` once the
// stream ends).
export async function readNdjsonStream<T>(response: Response, onEvent: (event: T) => void): Promise<void> {
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      if (line.trim()) onEvent(JSON.parse(line));
    }
  }
  if (buf.trim()) onEvent(JSON.parse(buf));
}
