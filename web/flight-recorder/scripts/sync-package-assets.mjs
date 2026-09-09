import {
  lstat,
  mkdir,
  mkdtemp,
  readdir,
  readFile,
  realpath,
  rename,
  rm,
  writeFile,
} from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const repositoryRoot = path.resolve(scriptDirectory, "../../..");
const ownedPackage = path.join(repositoryRoot, "src/agentic_saga/demo/static");
const defaults = {
  dist: path.resolve(scriptDirectory, "../dist"),
  package: ownedPackage,
};

async function listFiles(root, relative = "") {
  const directory = path.join(root, relative);
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const child = path.join(relative, entry.name);
    if (entry.isDirectory()) files.push(...(await listFiles(root, child)));
    else if (entry.isFile()) files.push(child);
    else throw new Error(`unsupported asset: ${child}`);
  }
  return files.sort();
}

async function sourceFiles(root) {
  await readFile(path.join(root, "index.html"));
  const assets = (await listFiles(path.join(root, "assets"))).map((file) => `assets/${file}`);
  return ["index.html", ...assets].sort();
}

async function sourceContents(root) {
  const files = await sourceFiles(root);
  return new Map(
    await Promise.all(files.map(async (file) => [file, await readFile(path.join(root, file))])),
  );
}

async function writeContents(contents, destination) {
  for (const [relative, bytes] of contents) {
    const target = path.join(destination, relative);
    await mkdir(path.dirname(target), { recursive: true });
    await writeFile(target, bytes, { flag: "wx" });
  }
}

async function contentsMatch(contents, destination) {
  const actual = await listFiles(destination);
  if ([...contents.keys()].join("\n") !== actual.join("\n")) return false;
  const matches = await Promise.all(
    [...contents].map(async ([relative, expected]) =>
      expected.equals(await readFile(path.join(destination, relative))),
    ),
  );
  return matches.every(Boolean);
}

function containsTraversal(rawPath) {
  return rawPath.split(/[\\/]+/u).includes("..");
}

async function rejectSymlinkAncestors(destination) {
  const parsed = path.parse(destination);
  let current = parsed.root;
  for (const segment of destination.slice(parsed.root.length).split(path.sep)) {
    current = path.join(current, segment);
    if ((await lstat(current)).isSymbolicLink()) {
      throw new Error("refusing package asset destination with symlink ancestor");
    }
  }
}

async function requireOwnedDestination(destination, rawDestination) {
  if (rawDestination !== undefined && containsTraversal(rawDestination)) {
    throw new Error("refusing package asset destination traversal");
  }
  if (path.resolve(destination) !== ownedPackage) {
    throw new Error("refusing unsafe package asset destination");
  }
  await rejectSymlinkAncestors(destination);
  const [actualRoot, actualDestination] = await Promise.all([
    realpath(repositoryRoot),
    realpath(destination),
  ]);
  if (actualRoot !== repositoryRoot || actualDestination !== ownedPackage) {
    throw new Error("refusing unsafe package asset destination");
  }
  if (!(await lstat(destination)).isDirectory()) {
    throw new Error("refusing non-directory package asset destination");
  }
}

async function replaceDirectory(destination, staged, renamePath) {
  const backup = `${staged}-backup`;
  await renamePath(destination, backup);
  try {
    await renamePath(staged, destination);
  } catch (replacementError) {
    try {
      await renamePath(backup, destination);
    } catch (rollbackError) {
      throw new AggregateError(
        [replacementError, rollbackError],
        "package asset replacement and rollback failed",
      );
    }
    throw replacementError;
  }
  await rm(backup, { recursive: true });
}

export async function writeAssets(source, destination, rawDestination, renamePath = rename) {
  await requireOwnedDestination(destination, rawDestination);
  const contents = await sourceContents(source);
  const staged = await mkdtemp(path.join(path.dirname(destination), ".static-stage-"));
  try {
    await writeContents(contents, staged);
    if (!(await contentsMatch(contents, staged))) {
      throw new Error("staged package assets failed byte verification");
    }
    await replaceDirectory(destination, staged, renamePath);
  } finally {
    await rm(staged, { force: true, recursive: true });
  }
}

async function checkAssets(source, destination) {
  return contentsMatch(await sourceContents(source), destination);
}

function options(argv) {
  const parsed = { ...defaults, rawPackage: undefined };
  const seen = new Set();
  for (let index = 1; index < argv.length; index += 2) {
    const key = argv[index]?.replace(/^--/u, "");
    const value = argv[index + 1];
    if ((key !== "dist" && key !== "package") || value === undefined || seen.has(key)) usage();
    seen.add(key);
    parsed[key] = path.resolve(value);
    if (key === "package") parsed.rawPackage = value;
  }
  return parsed;
}

function usage() {
  throw new Error("usage: sync-package-assets.mjs --write|--check [--dist PATH --package PATH]");
}

async function main(argv) {
  const mode = argv[0];
  const paths = options(argv);
  if (mode === "--write") await writeAssets(paths.dist, paths.package, paths.rawPackage);
  else if (mode === "--check") {
    if (!(await checkAssets(paths.dist, paths.package))) {
      throw new Error("package assets are out of sync");
    }
  } else usage();
}

if (path.resolve(process.argv[1] ?? "") === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch((error) => {
    process.stderr.write(`${error instanceof Error ? error.message : "asset sync failed"}\n`);
    process.exitCode = 1;
  });
}
