// Serves the golden file tests/test_golden.py checks `sourceledger report` against, for the
// test suite's sample sources. The test serves them on a free local port, shown as 8000.
// The banner and the noindex tag are added here, so the golden stays what the test compares.
import report from "../../../tests/golden/full/report.html?raw";

const banner =
  '<aside aria-label="Example notice" style="margin:0;padding:.5rem 1rem;background:#fff3c4;color:#1a1a1a;font:1rem/1.5 system-ui,sans-serif">' +
  "Example report made from the test suite's synthetic sources. " +
  '<a href="/sourceledger/reference.html#html-provenance-report" style="color:#0b3d91">Back to the reference</a></aside>';

const page = report.replace("<head>", '<head><meta name="robots" content="noindex">').replace("<body>", `<body>${banner}`);

export const GET = () => new Response(page, { headers: { "Content-Type": "text/html; charset=utf-8" } });
