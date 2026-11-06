// The golden file tests/test_golden.py checks `sourceledger report` against, for the test
// suite's sample sources. The test serves them on a free local port, shown as 8000, and
// shows each output's size and hash, which cover rows holding that port, as VARIES.
import report from "../../../tests/golden/full/report.html?raw";

export const GET = () => new Response(report, { headers: { "Content-Type": "text/html; charset=utf-8" } });
