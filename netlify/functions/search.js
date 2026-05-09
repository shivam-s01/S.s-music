const YT_API_KEY = process.env.YT_API_KEY;

export async function handler(event) {
  const q = event.queryStringParameters?.q || '';
  if (!q) return { statusCode: 400, body: JSON.stringify({ error: 'no query' }) };

  const resHeaders = {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*'
  };

  try {
    const url = `https://www.googleapis.com/youtube/v3/search?part=snippet&q=${encodeURIComponent(q)}&type=video&maxResults=1&key=${YT_API_KEY}`;
    const r = await fetch(url, { signal: AbortSignal.timeout(8000) });
    const d = await r.json();
    const videoId = d?.items?.[0]?.id?.videoId || null;
    return { statusCode: 200, headers: resHeaders, body: JSON.stringify({ videoId }) };
  } catch (e) {
    return { statusCode: 200, headers: resHeaders, body: JSON.stringify({ videoId: null }) };
  }
}
