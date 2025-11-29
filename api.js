import FormData from "form-data";
import fetch from "node-fetch";

export const config = {
  api: {
    bodyParser: false,
  },
};

function getBoundary(contentType) {
  const match = contentType.match(/boundary=(.*)$/);
  return match ? match[1] : null;
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(405).json({ error: "Only POST supported" });
    return;
  }

  const PROCESSOR_URL = process.env.PROCESSOR_URL;
  if (!PROCESSOR_URL) {
    res.status(500).json({ error: "PROCESSOR_URL env variable missing" });
    return;
  }

  try {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks);

    const contentType = req.headers["content-type"];
    const boundary = getBoundary(contentType);

    if (!boundary) {
      res.status(400).json({ error: "Invalid multipart/form-data" });
      return;
    }

    const boundaryBuf = Buffer.from("--" + boundary);
    const parts = [];

    let start = body.indexOf(boundaryBuf);
    while (start !== -1) {
      const next = body.indexOf(boundaryBuf, start + boundaryBuf.length);
      if (next === -1) break;
      const part = body.slice(start + boundaryBuf.length + 2, next - 2); 
      parts.push(part);
      start = next;
    }

    let fileBuffer = null;
    let filename = "file.pdf";

    for (const part of parts) {
      const headerEnd = part.indexOf("\r\n\r\n");
      if (headerEnd === -1) continue;

      const header = part.slice(0, headerEnd).toString("utf8");
      const fileMatch = header.match(/filename="(.+?)"/);

      if (fileMatch) {
        filename = fileMatch[1];
        fileBuffer = part.slice(headerEnd + 4);
        break;
      }
    }

    if (!fileBuffer) {
      res.status(400).json({ error: "No file found in request" });
      return;
    }

    const form = new FormData();
    form.append("file", fileBuffer, { filename });

    const resp = await fetch(PROCESSOR_URL + "/upload", {
      method: "POST",
      headers: form.getHeaders(),
      body: form,
    });

    const output = await resp.arrayBuffer();
    res.setHeader("Content-Type", resp.headers.get("content-type"));
    res.status(resp.status).send(Buffer.from(output));

  } catch (err) {
    console.error("UPLOAD ERROR:", err);
    res.status(500).json({ error: "Proxy failed", detail: String(err) });
  }
}
