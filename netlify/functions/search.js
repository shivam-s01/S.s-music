export async function handler(event) {
  const q = event.queryStringParameters?.q || '';
  if (!q) return { statusCode: 400, body: JSON.stringify({ error: 'no query' }) };

  const headers = { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' };

  const PIPED = [
    'https://pipedapi.kavin.rocks',
    'https://pipedapi.adminforge.de',
    'https://piped-api.garudalinux.org',
  ];
  const INVIDIOUS = [
    'https://inv.riverside.rocks',
    'https://yt.cdaut.de',
  ];

  // Try Piped nodes
  for (const node of PIPED) {
    try {
      const r = await fetch(`${node}/search?q=${q}&filter=videos`, { signal: AbortSignal.timeout(6000) });
      if (!r.ok) continue;
      const d = await r.json();
      const url = d?.items?.[0]?.url || '';
      const vid = url.includes('v=') ? url.split('v=')[1].split('&')[0] : null;
      if (vid) return { statusCode: 200, headers, body: JSON.stringify({ videoId: vid }) };
    } catch { continue; }
  }

  // Fallback: Invidious
  for (const node of INVIDIOUS) {
    try {
      const r = await fetch(`${node}/api/v1/search?q=${q}&type=video&fields=videoId`, { signal: AbortSignal.timeout(6000) });
      if (!r.ok) continue;
      const d = await r.json();
      const vid = Array.isArray(d) ? d[0]?.videoId : null;
      if (vid) return { statusCode: 200, headers, body: JSON.stringify({ videoId: vid }) };
    } catch { continue; }
  }

  return { statusCode: 200, headers, body: JSON.stringify({ videoId: null }) };
}
