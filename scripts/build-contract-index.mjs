import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

await mkdir(resolve("dist"), { recursive: true });

await writeFile(
  resolve("dist/index.html"),
  `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width">
  <title>Component Contracts</title>
  <style>
    :root { color-scheme: light dark; }
    body { max-width: 760px; margin: 64px auto; padding: 24px; font: 18px system-ui, sans-serif; line-height: 1.6; }
    h1 { margin-bottom: 8px; }
    p { margin-top: 0; }
    ul { padding-left: 24px; }
    li { margin: 16px 0; }
    a { color: #1264a3; }
  </style>
</head>
<body>
  <h1>Component contracts</h1>
  <p>Generated documentation for the platform's HTTP, event, and internal interfaces.</p>
  <ul>
    <li><a href="internal/">Overview — Contract conventions</a></li>
    <li><a href="openapi/">OpenAPI — HTTP APIs</a></li>
    <li><a href="asyncapi/">AsyncAPI — event contracts</a></li>
    <li><a href="internal/">Internal interfaces</a></li>
  </ul>
</body>
</html>
`,
  "utf8",
);
