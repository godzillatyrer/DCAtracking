// Tiny mock of https://ansem.io/api/coins so the gold/diamond path can be exercised
// even while the live index has zero Gold/Diamond listings.
// Usage: node test/mock-api.mjs <scenarioFile> <port>
import http from 'node:http';
import fs from 'node:fs';

const scenarioFile = process.argv[2];
const port = Number(process.argv[3] || 8787);

http.createServer((req, res) => {
  const coins = JSON.parse(fs.readFileSync(scenarioFile, 'utf8'));
  res.writeHead(200, { 'content-type': 'application/json' });
  res.end(JSON.stringify({ coins }));
}).listen(port, () => console.log(`mock api on ${port} serving ${scenarioFile}`));
