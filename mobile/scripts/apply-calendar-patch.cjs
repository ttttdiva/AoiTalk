const fs = require("node:fs");
const path = require("node:path");

const mobileRoot = path.resolve(__dirname, "..");
const packageRoot = path.join(mobileRoot, "node_modules", "react-native-calendars");
const packageJsonPath = path.join(packageRoot, "package.json");
const patchPath = path.join(
  mobileRoot,
  "patches",
  "react-native-calendars+1.1314.0.patch",
);
const targetPath = path.join(
  packageRoot,
  "src",
  "calendar-list",
  "index.js",
);

if (!fs.existsSync(packageJsonPath) || !fs.existsSync(patchPath)) {
  throw new Error("Calendar dependency or tracked patch is missing");
}
const version = JSON.parse(fs.readFileSync(packageJsonPath, "utf8")).version;
if (version !== "1.1314.0") {
  throw new Error(
    `Calendar patch expects react-native-calendars@1.1314.0, found ${version}`,
  );
}

const oldRange = "const range = useRef(horizontal ? 1 : 3);";
const newRange = "const range = useRef(horizontal ? 2 : 3);";
const oldViewability = [
  "        const newVisibleMonth = parseDate(viewableItems[0]?.item);",
  "        if (shouldFixRTL) {",
].join("\n");
const newViewability = [
  "        const newVisibleMonth = parseDate(viewableItems[0]?.item);",
  "        if (!newVisibleMonth) {",
  "            return;",
  "        }",
  "        if (shouldFixRTL) {",
].join("\n");
const trackedChanges = fs
  .readFileSync(patchPath, "utf8")
  .replace(/\r\n/g, "\n")
  .split("\n")
  .filter((line) => /^[-+][^+-]/.test(line));
const expectedChanges = [
  `-    ${oldRange}`,
  `+    ${newRange}`,
  "+        if (!newVisibleMonth) {",
  "+            return;",
  "+        }",
];
if (JSON.stringify(trackedChanges) !== JSON.stringify(expectedChanges)) {
  throw new Error("Tracked Calendar patch does not match the apply guard");
}

const source = fs.readFileSync(targetPath, "utf8").replace(/\r\n/g, "\n");
const hasNewRange = source.includes(newRange);
const hasNewViewability = source.includes(newViewability);
if (hasNewRange && hasNewViewability) {
  console.log("react-native-calendars CalendarList patch already applied");
  process.exit(0);
}
if (!source.includes(oldRange) || !source.includes(oldViewability)) {
  throw new Error("Calendar patch does not match the installed source");
}

const patched = source
  .replace(oldRange, newRange)
  .replace(oldViewability, newViewability);
if (patched === source) throw new Error("Calendar patch made no change");
fs.writeFileSync(targetPath, patched, "utf8");
console.log("Applied react-native-calendars CalendarList patch");
