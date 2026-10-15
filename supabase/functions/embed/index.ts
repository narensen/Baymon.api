// Supabase Edge Function: text -> 384-dim embeddings with the built-in gte-small model.
//
// POST /functions/v1/embed
//   headers: x-embed-secret: <EMBED_SECRET>
//   body:    {"input": "text" | ["text", ...]}   (max 64 texts)
//   reply:   {"embeddings": number[][], "model": "gte-small", "dimensions": 384}
//
// Deploy with JWT verification OFF (the Baymon API authenticates with the shared
// secret instead) and set the EMBED_SECRET function secret.

const session = new Supabase.ai.Session("gte-small");
const MAX_TEXTS = 64;
const MAX_CHARS = 2000; // gte-small reads ~512 tokens

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "method not allowed" }, 405);

  const secret = Deno.env.get("EMBED_SECRET");
  if (!secret || req.headers.get("x-embed-secret") !== secret) return json({ error: "unauthorized" }, 401);

  let input: unknown;
  try {
    ({ input } = await req.json());
  } catch {
    return json({ error: "body must be JSON" }, 400);
  }
  const texts = Array.isArray(input) ? input : [input];
  if (texts.length === 0 || texts.length > MAX_TEXTS || texts.some((t) => typeof t !== "string")) {
    return json({ error: `input must be a string or 1-${MAX_TEXTS} strings` }, 400);
  }

  const embeddings: number[][] = [];
  for (const text of texts as string[]) {
    const vector = await session.run(text.slice(0, MAX_CHARS) || " ", { mean_pool: true, normalize: true });
    embeddings.push(Array.from(vector as number[]));
  }
  return json({ embeddings, model: "gte-small", dimensions: 384 });
});
