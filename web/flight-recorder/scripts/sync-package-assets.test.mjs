import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import {
  copyFile,
  mkdir,
  mkdtemp,
  readdir,
  readFile,
  realpath,
  rename,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import process from "node:process";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const script = fileURLToPath(new URL("sync-package-assets.mjs", import.meta.url));

async function temporaryRoot(context) {
  const root = await mkdtemp(path.join(await realpath(tmpdir()), "agentic-saga-assets-"));
  context.after(() => rm(root, { force: true, recursive: true }));
  return root;
}

async function isolatedRepository(context) {
  const root = await temporaryRoot(context);
  const isolatedScript = path.join(
    root,
    "repository/web/flight-recorder/scripts/sync-package-assets.mjs",
  );
  const source = path.join(root, "repository/web/flight-recorder/dist");
  const destination = path.join(root, "repository/src/agentic_saga/demo/static");
  await Promise.all([
    mkdir(path.dirname(isolatedScript), { recursive: true }),
    mkdir(source, { recursive: true }),
    mkdir(destination, { recursive: true }),
  ]);
  await copyFile(script, isolatedScript);
  return { destination, script: isolatedScript, source };
}

async function runAssetScript(mode, source, destination, executable = script) {
  const arguments_ = [executable, mode, "--dist", source, "--package", destination];
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, arguments_, { stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.once("error", reject);
    child.once("close", (code) => resolve({ code, stderr }));
  });
}

async function createSource(source) {
  await mkdir(path.join(source, "assets"), { recursive: true });
  await writeFile(path.join(source, "index.html"), Buffer.from("replacement"));
}

async function packageFiles(root, relative = "") {
  const entries = await readdir(path.join(root, relative), { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const child = path.join(relative, entry.name);
    if (entry.isDirectory()) files.push(...Object.entries(await packageFiles(root, child)));
    else if (entry.isFile()) files.push([child, await readFile(path.join(root, child))]);
  }
  return Object.fromEntries(files.sort(([first], [second]) => first.localeCompare(second)));
}

test("write replaces only the owned package with exact index and asset bytes", async (context) => {
  const isolated = await isolatedRepository(context);
  await createSource(isolated.source);
  await mkdir(path.join(isolated.source, "traces"));
  await writeFile(path.join(isolated.source, "assets/app.js"), "export const ready = true;");
  await writeFile(path.join(isolated.source, "traces/private.json"), "{}");
  await writeFile(path.join(isolated.destination, "stale.js"), "stale");

  const result = await runAssetScript(
    "--write",
    isolated.source,
    isolated.destination,
    isolated.script,
  );

  assert.equal(result.code, 0, result.stderr);
  assert.deepEqual(await packageFiles(isolated.destination), {
    "assets/app.js": Buffer.from("export const ready = true;"),
    "index.html": Buffer.from("replacement"),
  });
  const check = await runAssetScript(
    "--check",
    isolated.source,
    isolated.destination,
    isolated.script,
  );
  assert.equal(check.code, 0, check.stderr);
});

test("legacy sync cannot delete an unrelated static directory", async (context) => {
  const root = await temporaryRoot(context);
  const source = path.join(root, "dist");
  const destination = path.join(root, "user/static");
  await createSource(source);
  await mkdir(destination, { recursive: true });
  const marker = path.join(destination, "keep.txt");
  await writeFile(marker, "must survive");

  const result = await runAssetScript("--sync", source, destination);

  assert.notEqual(result.code, 0);
  assert.equal(await readFile(marker, "utf8"), "must survive");
});

test("write refuses an unrelated static directory without deleting it", async (context) => {
  const isolated = await isolatedRepository(context);
  const destination = path.join(await temporaryRoot(context), "user/static");
  await createSource(isolated.source);
  await mkdir(destination, { recursive: true });
  const marker = path.join(destination, "keep.txt");
  await writeFile(marker, "must survive");

  const result = await runAssetScript("--write", isolated.source, destination, isolated.script);

  assert.notEqual(result.code, 0);
  assert.match(result.stderr, /refusing unsafe package asset destination/i);
  assert.equal(await readFile(marker, "utf8"), "must survive");
});

test("write refuses traversal even when it resolves to the owned path", async (context) => {
  const isolated = await isolatedRepository(context);
  await createSource(isolated.source);
  const marker = path.join(isolated.destination, "keep.txt");
  await writeFile(marker, "must survive");
  const traversing = `${path.dirname(isolated.destination)}/ignored/../static`;

  const result = await runAssetScript("--write", isolated.source, traversing, isolated.script);

  assert.notEqual(result.code, 0);
  assert.match(result.stderr, /traversal/i);
  assert.equal(await readFile(marker, "utf8"), "must survive");
});

test("write refuses a symlinked destination ancestor", async (context) => {
  const root = await temporaryRoot(context);
  const repository = path.join(root, "repository");
  const isolatedScript = path.join(
    repository,
    "web/flight-recorder/scripts",
    path.basename(script),
  );
  const source = path.join(repository, "web/flight-recorder/dist");
  const outside = path.join(root, "outside");
  const destination = path.join(repository, "src/agentic_saga/demo/static");
  const outsideDestination = path.join(outside, "agentic_saga/demo/static");
  await Promise.all([
    mkdir(path.dirname(isolatedScript), { recursive: true }),
    mkdir(outsideDestination, { recursive: true }),
    createSource(source),
  ]);
  await copyFile(script, isolatedScript);
  await symlink(outside, path.join(repository, "src"), "dir");
  const marker = path.join(outsideDestination, "keep.txt");
  await writeFile(marker, "must survive");

  const result = await runAssetScript("--write", source, destination, isolatedScript);

  assert.notEqual(result.code, 0);
  assert.match(result.stderr, /symlink/i);
  assert.equal(await readFile(marker, "utf8"), "must survive");
});

test("write preserves the current package when a source asset is unsafe", async (context) => {
  const isolated = await isolatedRepository(context);
  await createSource(isolated.source);
  await symlink("missing.js", path.join(isolated.source, "assets/escape.js"));
  await writeFile(path.join(isolated.destination, "keep.txt"), "must survive");

  const result = await runAssetScript(
    "--write",
    isolated.source,
    isolated.destination,
    isolated.script,
  );

  assert.notEqual(result.code, 0);
  assert.match(result.stderr, /unsupported asset/i);
  assert.deepEqual(await packageFiles(isolated.destination), {
    "keep.txt": Buffer.from("must survive"),
  });
});

test("write restores exact original bytes when staged promotion fails", async (context) => {
  const isolated = await isolatedRepository(context);
  await createSource(isolated.source);
  await writeFile(path.join(isolated.source, "assets/replacement.js"), "replacement");
  await mkdir(path.join(isolated.destination, "assets"));
  await writeFile(path.join(isolated.destination, "index.html"), "original index");
  await writeFile(path.join(isolated.destination, "assets/original.js"), "original asset");
  const assetModule = await import(`${pathToFileURL(isolated.script).href}?rollback`);
  let renameCount = 0;
  const failPromotion = async (source, destination) => {
    renameCount += 1;
    if (renameCount === 2) throw new Error("injected staged promotion failure");
    await rename(source, destination);
  };

  await assert.rejects(
    assetModule.writeAssets(isolated.source, isolated.destination, undefined, failPromotion),
    /injected staged promotion failure/,
  );

  assert.equal(renameCount, 3);
  assert.deepEqual(await packageFiles(isolated.destination), {
    "assets/original.js": Buffer.from("original asset"),
    "index.html": Buffer.from("original index"),
  });
  assert.deepEqual(await readdir(path.dirname(isolated.destination)), ["static"]);
});

test("check rejects byte drift in the packaged assets", async (context) => {
  const root = await temporaryRoot(context);
  const source = path.join(root, "dist");
  const destination = path.join(root, "static");
  await createSource(source);
  await mkdir(path.join(destination, "assets"), { recursive: true });
  await writeFile(path.join(source, "assets/app.js"), "reviewed");
  await writeFile(path.join(destination, "assets/app.js"), "drifted");
  await writeFile(path.join(destination, "index.html"), "replacement");

  const result = await runAssetScript("--check", source, destination);

  assert.notEqual(result.code, 0);
  assert.match(result.stderr, /out of sync/i);
});
