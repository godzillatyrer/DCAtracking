// Tiny mock of the ansem.io API so paths the live site exposes can be exercised
// offline — the gold/diamond feed (which currently has zero Gold/Diamond coins),
// plus the burn, config, listing and gate side channels.
//
// Usage: node test/mock-api.mjs <scenarioFile> <port>
//
// /api/coins             <- {coins: <scenarioFile>}
// /api/leaderboard/burners <- {burners: <dir>/burners.json}   404 if absent
// /api/config            <- <dir>/config.json                 404 if absent
// /api/listing/config    <- <dir>/listing.json                404 if absent
// /api/gate              <- <dir>/gate.json                   404 if absent
//
// Absent-by-default is deliberate: it exercises the path where a side channel
// does not exist and must degrade quietly instead of breaking the tier watcher.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';

const scenarioFile = process.argv[2];
const port = Number(process.argv[3] || 8787);
const dir = path.dirname(scenarioFile);

const sidecar = (name) => {
  try {
    return JSON.parse(fs.readFileSync(path.join(dir, name), 'utf8'));
  } catch {
    return null;
  }
};

http.createServer((req, res) => {
  const url = req.url.split('?')[0];
  const send = (code, obj) => {
    res.writeHead(code, { 'content-type': 'application/json' });
    res.end(JSON.stringify(obj));
  };

  if (url.endsWith('/leaderboard/burners')) {
    const burners = sidecar('burners.json');
    return burners ? send(200, { burners }) : send(404, { error: 'not found' });
  }
  if (url.endsWith('/listing/config')) {
    const listing = sidecar('listing.json');
    return listing ? send(200, listing) : send(404, { error: 'not found' });
  }
  if (url.endsWith('/gate')) {
    const gate = sidecar('gate.json');
    return gate ? send(200, gate) : send(404, { error: 'not found' });
  }
  if (url.endsWith('/config')) {
    const cfg = sidecar('config.json');
    return cfg ? send(200, cfg) : send(404, { error: 'not found' });
  }

  send(200, { coins: JSON.parse(fs.readFileSync(scenarioFile, 'utf8')) });
}).listen(port, () => console.log(`mock api on ${port} serving ${scenarioFile}`));
