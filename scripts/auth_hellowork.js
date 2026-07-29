#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const readline = require("readline");
const { chromium } = require("playwright");

function outputPath() {
  const index = process.argv.indexOf("--output");
  if (index !== -1 && process.argv[index + 1]) {
    return path.resolve(process.argv[index + 1]);
  }
  return path.resolve("hellowork-auth.json");
}

function waitForEnter(message) {
  const prompt = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
  });
  return new Promise((resolve) => prompt.question(message, () => {
    prompt.close();
    resolve();
  }));
}

async function main() {
  const target = outputPath();
  fs.mkdirSync(path.dirname(target), { recursive: true });

  const browser = await chromium.launch({ headless: false });
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto("https://www.hellowork.com/fr-fr/candidat/connexion.html");
    await waitForEnter(
      "Sign in to HelloWork in the browser, complete any verification, then press Enter here: "
    );
    await context.storageState({ path: target });
  } finally {
    await browser.close();
  }

  console.log(`HelloWork authentication saved to ${target}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
