export async function handler(event) {
  const q = event.queryStringParameters?.q || '';
  if (!q) return { statusCode: 400, body: JSON.stringify({ error: 'no query' }) };

  const resHeaders = {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*'
  };

  try {
    const searchUrl = `https://www.youtube.com/results?search_query=${encodeURIComponent(q)}`;
    const r = await fetch(searchUrl, {
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept': 'text/html,application/xhtml+xml'
      },
      signal: AbortSignal.timeout(8000)
    });

    const html = await r.text();

    // YouTube embeds all video data as JSON inside the page HTML
    // First "videoId" match is the top search result
    const match = html.match(/"videoId":"([a-zA-Z0-9_-]{11})"/);
    if (match && match[1]) {
      return {
        statusCode: 200,
        headers: resHeaders,
        body: JSON.stringify({ videoId: match[1] })
      };
    }
  } catch (e) {
    console.error('YouTube scrape failed:', e.message);
  }

  return {
    statusCode: 200,
    headers: resHeaders,
    body: JSON.stringify({ videoId: null })
  };
}
