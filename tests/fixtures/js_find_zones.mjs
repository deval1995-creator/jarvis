// Runs the Tempo desk's original findZones on bars from stdin (for tests/test_zones.py).
const DESK = (process.env.TEMPO_DESK || 'C:/Users/deval/OneDrive/Desktop/Tempo_Automation/desk').split('\\').join('/');
const { findZones } = await import(`file:///${DESK}/src/zones.js`);
let input = '';
for await (const c of process.stdin) input += c;
const cases = JSON.parse(input);
process.stdout.write(JSON.stringify(cases.map((c) => findZones(c.bars, c.opt))));
process.exit(0);
