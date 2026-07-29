#!/usr/bin/env node
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const readline = require("readline");
const { spawn } = require("child_process");
const { chromium } = require("playwright");

const LOGIN_URL = "https://www.hellowork.com/fr-fr/candidat/connexion.html";
const DEBUG_PORT = 9222;

function argument(name, fallback) {
  const index = process.argv.indexOf(name);
  return index !== -1 && process.argv[index + 1]
    ? process.argv[index + 1]
    : fallback;
}

function findChrome() {
  const candidates = [
    process.env.PROGRAMFILES &&
      path.join(process.env.PROGRAMFILES, "Google", "Chrome", "Application", "chrome.exe"),
    process.env["PROGRAMFILES(X86)"] &&
      path.join(process.env["PROGRAMFILES(X86)"], "Google", "Chrome", "Application", "chrome.exe"),
    process.env.LOCALAPPDATA &&
      path.join(process.env.LOCALAPPDATA, "Google", "Chrome", "Application", "chrome.exe"),
  ].filter(Boolean);

  const chrome = candidates.find((candidate) => fs.existsSync(candidate));
  if (!chrome) {
    throw new Error("Google Chrome was not found. Install Chrome and run this file again.");
  }
  return chrome;
}

function waitForEnter(message) {
  const prompt = readline.createInterface({ input: process.stdin, output: process.stdout });
  return new Promise((resolve) =>
    prompt.question(message, () => {
      prompt.close();
      resolve();
    })
  );
}

async function connect() {
  const endpoint = `http://127.0.0.1:${DEBUG_PORT}`;
  let lastError;
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      return await chromium.connectOverCDP(endpoint);
    } catch (error) {
      lastError = error;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }
  throw lastError;
}

async function main() {
  const target = path.resolve(argument("--output", "hellowork-auth.json"));
  const profile = path.resolve(
    argument("--profile", path.join(os.tmpdir(), "getajob-hellowork-chrome"))
  );
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.mkdirSync(profile, { recursive: true });

  const chrome = spawn(
    findChrome(),
    [
      `--remote-debugging-port=${DEBUG_PORT}`,
      `--user-data-dir=${profile}`,
      "--no-first-run",
      "--no-default-browser-check",
      LOGIN_URL,
    ],
    { detached: true, stdio: "ignore" }
  );
  chrome.unref();

  await waitForEnter(
    "Log in completely in the normal Chrome window. Only after HelloWork opens, press Enter here: "
  );

  const browser = await connect();
  try {
    const contexts = browser.contexts();
    if (!contexts.length) {
      throw new Error("Chrome has no open browser context.");
    }
    await contexts[0].storageState({ path: target });
  } finally {
    await browser.close();
  }

  console.log(`HelloWork authentication saved to ${target}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
