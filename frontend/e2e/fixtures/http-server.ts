import {
  createServer,
  type IncomingMessage,
  type ServerResponse,
} from "node:http";

export type FixtureHttpServer = {
  origin: string;
  close: () => Promise<void>;
};

export type FixtureRequestHandler = (
  request: IncomingMessage,
  response: ServerResponse,
) => void | Promise<void>;

export async function startFixtureHttpServer(
  handler: FixtureRequestHandler,
): Promise<FixtureHttpServer> {
  const server = createServer((request, response) => {
    void Promise.resolve(handler(request, response)).catch(() => {
      if (!response.headersSent) {
        response.writeHead(500, {
          "Cache-Control": "no-store",
          Connection: "close",
          "Content-Type": "text/plain; charset=utf-8",
        });
      }
      response.end("fixture request failed");
    });
  });

  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve();
    });
  });

  const address = server.address();
  if (!address || typeof address === "string") {
    await new Promise<void>((resolve) => server.close(() => resolve()));
    throw new Error("fixture server did not receive a loopback port");
  }

  return {
    origin: `http://127.0.0.1:${address.port}`,
    close: async () => {
      await new Promise<void>((resolve, reject) => {
        server.close((error) => (error ? reject(error) : resolve()));
      });
    },
  };
}

export async function readFixtureRequestBody(
  request: IncomingMessage,
): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  return Buffer.concat(chunks);
}
