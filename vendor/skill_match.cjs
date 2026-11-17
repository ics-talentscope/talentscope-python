// src/lib/skill-match.ts
var CANONICAL_ALIASES = {
  js: "javascript",
  javascript: "javascript",
  typescript: "typescript",
  ts: "typescript",
  python: "python",
  py: "python",
  java: "java",
  "core java": "java",
  "c++": "c++",
  cpp: "c++",
  "c#": "c#",
  csharp: "c#",
  golang: "go",
  go: "go",
  node: "node.js",
  nodejs: "node.js",
  "node.js": "node.js",
  react: "react",
  reactjs: "react",
  angular: "angular",
  vue: "vue",
  vuejs: "vue",
  "next.js": "next.js",
  nextjs: "next.js",
  express: "express",
  django: "django",
  flask: "flask",
  spring: "spring",
  springboot: "spring",
  "spring boot": "spring",
  sql: "sql",
  postgresql: "postgresql",
  postgres: "postgresql",
  oracle: "oracle",
  mysql: "mysql",
  "sql server": "sql server",
  mssql: "sql server",
  mongodb: "mongodb",
  mongo: "mongodb",
  redis: "redis",
  aws: "aws",
  "amazon web services": "aws",
  eks: "eks",
  ec2: "ec2",
  lambda: "lambda",
  "api gateway": "api gateway",
  cloudfront: "cloudfront",
  rds: "rds",
  iam: "iam",
  vpc: "vpc",
  s3: "s3",
  "route 53": "route 53",
  route53: "route 53",
  elb: "elb",
  alb: "alb",
  "auto scaling": "auto scaling",
  autoscaling: "auto scaling",
  waf: "waf",
  cloudwatch: "cloudwatch",
  bitbucket: "bitbucket",
  fargate: "fargate",
  azure: "azure",
  azue: "azure",
  gcp: "gcp",
  "google cloud": "gcp",
  docker: "docker",
  kubernetes: "kubernetes",
  k8s: "kubernetes",
  jenkins: "jenkins",
  jekins: "jenkins",
  terraform: "terraform",
  ansible: "ansible",
  "ci/cd": "ci/cd",
  cicd: "ci/cd",
  "ci-cd": "ci/cd",
  git: "git",
  github: "github",
  gitlab: "gitlab",
  linux: "linux",
  rhel: "linux",
  "red hat": "linux",
  groovy: "groovy",
  qualys: "qualys",
  podman: "podman",
  vmware: "vmware",
  unix: "unix",
  windows: "windows",
  bash: "bash",
  shell: "bash",
  "shell script": "bash",
  "shell scripting": "bash",
  powershell: "powershell",
  splunk: "splunk",
  datadog: "datadog",
  dynatrace: "dynatrace",
  servicenow: "servicenow",
  "service now": "servicenow",
  jira: "jira",
  selenium: "selenium",
  playwright: "playwright",
  cypress: "cypress",
  cyress: "cypress",
  postman: "postman",
  kafka: "kafka",
  rest: "rest",
  "rest apis": "rest apis",
  "rest api": "rest apis",
  restful: "rest apis",
  "restful webservices": "rest apis",
  "restful web services": "rest apis",
  api: "api",
  apis: "api",
  microservices: "microservices",
  hibernate: "hibernate",
  html: "html",
  css: "css",
  cyberark: "cyberark",
  tibco: "tibco",
  mulesoft: "mulesoft",
  "mule soft": "mulesoft",
  cmm: "cmm",
  cmg: "cmg",
  mme: "mme",
  paco: "paco",
  nokia: "nokia",
  "cloud mobility manager": "cmm",
  "cloud mobile gateway": "cmg",
  servlet: "servlet",
  springframework: "spring",
  "spring framework": "spring",
  wireshark: "wireshark",
  pcap: "pcap",
  helm: "helm",
  "helm chart": "helm",
  istio: "istio",
  openshift: "openshift",
  openstack: "openstack",
  caas: "caas",
  cnf: "cnf",
  vnf: "vnf",
  lld: "lld",
  hld: "hld",
  pcrf: "pcrf",
  pcef: "pcef",
  hss: "hss",
  hlr: "hlr",
  udm: "udm",
  nds: "nds",
  sdl: "sdl",
  sdm: "sdm",
  "subscriber data management": "sdm",
  ims: "ims",
  ntas: "ntas",
  cfx: "cfx",
  sbc: "sbc",
  diameter: "diameter",
  sip: "sip",
  gtp: "gtp",
  sctp: "sctp",
  http2: "http/2",
  "http/2": "http/2",
  "http 2": "http/2",
  "http-2": "http/2",
  "robot framework": "robot framework",
  robot: "robot framework",
  yocto: "yocto",
  etl: "etl",
  ".net": ".net",
  dotnet: ".net",
  "asp.net": ".net",
  "asp.net core": ".net",
  "net core": ".net"
};
var RELATED_EQUIVALENCE = {
  unix: ["linux"],
  linux: ["unix", "bash"],
  bash: ["linux", "shell"],
  spring: ["java", "j2ee"],
  java: ["spring", "j2ee"],
  j2ee: ["java", "spring"],
  kubernetes: ["openshift", "caas", "eks"],
  eks: ["kubernetes"],
  fargate: ["docker"],
  alb: ["elb"],
  elb: ["alb"],
  bitbucket: ["gitlab", "git"],
  openshift: ["kubernetes"],
  openstack: ["caas"],
  helm: ["kubernetes"],
  istio: ["kubernetes"],
  cnf: ["kubernetes", "vnf"],
  vnf: ["cnf"],
  hss: ["hlr", "udm", "sdm"],
  hlr: ["hss", "sdm"],
  udm: ["hss", "sdm"],
  sdm: ["hss", "hlr", "udm", "nds", "sdl"],
  nds: ["sdm", "sdl"],
  sdl: ["sdm", "nds"],
  ims: ["sip", "ntas", "cfx", "sbc"],
  ntas: ["ims", "cfx"],
  cfx: ["ims", "sbc"],
  sbc: ["ims", "cfx"],
  pcrf: ["pcef", "diameter"],
  pcef: ["pcrf"],
  ".net": ["c#"],
  "c#": [".net"]
};
var WEAK_BODY_SKILLS = /* @__PURE__ */ new Set([
  "api",
  "testing",
  "automation",
  "architecture",
  "git",
  "html",
  "css",
  "rest"
]);
var TABLE_STAKES_SKILLS = /* @__PURE__ */ new Set([
  "git",
  "github",
  "html",
  "css",
  "rest",
  "api"
]);
var NON_TECHNICAL_SKILLS = /* @__PURE__ */ new Set([
  "jira",
  "agile",
  "scrum",
  "kanban",
  "sdlc",
  "waterfall",
  "confluence",
  "excel",
  "servicenow",
  "itil",
  "banking",
  "capital markets",
  "capital market",
  "pmp",
  "manual testing"
]);
function isNonTechnicalSkill(skill) {
  const canon = canonicalizeToken(skill) || skill;
  return NON_TECHNICAL_SKILLS.has(canon);
}
function isTechnicalFamily(family) {
  return family !== "manager" && family !== "support" && family !== "other";
}
var FE_FRAMEWORKS = /* @__PURE__ */ new Set(["react", "angular", "vue", "next.js"]);
var BE_LANGUAGES = /* @__PURE__ */ new Set([
  "java",
  "python",
  "node.js",
  "c#",
  "go",
  "spring",
  "django",
  "flask",
  "express",
  ".net"
]);
var TELECOM_CORE = /* @__PURE__ */ new Set([
  "pcrf",
  "pcef",
  "hss",
  "hlr",
  "udm",
  "nds",
  "sdl",
  "sdm",
  "ims",
  "ntas",
  "cfx",
  "sbc",
  "diameter",
  "sip",
  "gtp",
  "sctp",
  "cmg",
  "cmm",
  "mme",
  "paco"
]);
var CLOUD_CORE = /* @__PURE__ */ new Set([
  "kubernetes",
  "openshift",
  "openstack",
  "aws",
  "azure",
  "gcp",
  "docker",
  "helm",
  "istio",
  "cnf",
  "vnf",
  "caas"
]);
var DEVOPS_CORE = /* @__PURE__ */ new Set([
  "devops",
  "ansible",
  "jenkins",
  "docker",
  "kubernetes",
  "linux",
  "terraform",
  "groovy",
  "podman",
  "rhel"
]);
var DEVOPS_NOISE = /* @__PURE__ */ new Set([
  "selenium",
  "copilot",
  "gemini",
  "java",
  "c++",
  "playwright",
  "react",
  "angular"
]);
var LINUX_CORE = /* @__PURE__ */ new Set(["linux", "bash", "vmware", "qualys", "podman"]);
var LINUX_NOISE = /* @__PURE__ */ new Set([
  "java",
  "c++",
  "gemini",
  "claude",
  "cursor",
  "copilot",
  "selenium",
  "playwright",
  "react",
  "angular"
]);
var QA_CORE = /* @__PURE__ */ new Set(["playwright", "selenium", "cypress", "robot framework", "python"]);
var QA_NOISE = /* @__PURE__ */ new Set([
  "kubernetes",
  "azure",
  "docker",
  "java",
  "c++",
  "gemini",
  "claude",
  "cursor",
  "copilot",
  "react",
  "angular",
  "ansible"
]);
var STOP_WORDS = /* @__PURE__ */ new Set([
  "to",
  "and",
  "the",
  "for",
  "in",
  "of",
  "on",
  "with",
  "at",
  "by",
  "from",
  "an",
  "is",
  "as",
  "end",
  "be",
  "or",
  "exp",
  "year",
  "years",
  "total",
  "skills",
  "skill",
  "basics",
  "basic",
  "etc",
  "ex",
  "eg",
  "employee",
  "general",
  "role",
  "manager",
  "engineer",
  "project",
  "team",
  "support",
  "experience",
  "mandatory",
  "required",
  "plus",
  "strong",
  "hands",
  "using"
]);
var PHRASE_CANONICALS = Object.keys(CANONICAL_ALIASES).filter((k) => k.includes(" ") || k.includes(".") || k.includes("/") || k.length > 3).sort((a, b) => b.length - a.length);
var EMPTY_SCORE_PARTS = {
  coveragePct: 0,
  familyScore: 0,
  stackScore: 0,
  levelScore: 0,
  years: null,
  grade: null,
  weighted: { coverage: 0, family: 0, stack: 0, level: 0 }
};
function emptyMatch(rationale = "No skills or requirement text to score.") {
  return {
    score: 0,
    strictRequirementsPassed: false,
    matchingSkills: [],
    matchedCount: 0,
    requiredCount: 0,
    decision: "reject",
    rationale,
    familyRelation: "mismatch",
    personFamily: "other",
    jdFamily: "other",
    skillBreakdown: [],
    bonusSkills: [],
    scoreParts: EMPTY_SCORE_PARTS
  };
}
function escapeRe(s) {
  return s.replace(/[-\/\\^$*+?.()|[\]{}]/g, "\\$&");
}
function normalizeProfileText(text) {
  return String(text || "").replace(/\bjava[\s_-]*scripts?\b/gi, "javascript").replace(/\s+/g, " ").trim().toLowerCase();
}
function hasPhrase(haystack, needle) {
  if (!needle) return false;
  const escaped = escapeRe(needle.toLowerCase());
  if (needle.toLowerCase() === "java") {
    return new RegExp(`(^|[^a-zA-Z0-9_#+])java(?![\\s_-]*script)([^a-zA-Z0-9_#+]|$)`, "i").test(haystack);
  }
  if (needle.toLowerCase() === "react") {
    return new RegExp(`(^|[^a-zA-Z0-9_#+])react(?!\\s*native)([^a-zA-Z0-9_#+]|$)`, "i").test(haystack);
  }
  const re = new RegExp(`(^|[^a-zA-Z0-9_#+])${escaped}([^a-zA-Z0-9_#+]|$)`, "i");
  return re.test(haystack);
}
function canonicalizeToken(raw) {
  let t = raw.toLowerCase().replace(/\s+/g, " ").trim();
  t = t.replace(/^(core|advanced|strong|hands[-\s]on)\s+/g, "");
  if (!t) return "";
  if (CANONICAL_ALIASES[t]) return CANONICAL_ALIASES[t];
  const compact = t.replace(/[\s._-]/g, "");
  if (CANONICAL_ALIASES[compact]) return CANONICAL_ALIASES[compact];
  return t;
}
function splitSkillList(line) {
  const protectedLine = line.replace(/\bci\s*\/\s*cd\b/gi, "ci-cd").replace(/\bhttp\s*\/\s*2\b/gi, "http-2");
  return protectedLine.split(/[,;|]+/).map((s) => s.replace(/\s+/g, " ").trim()).filter((s) => s.length >= 2 && !STOP_WORDS.has(s.toLowerCase()));
}
function acronymsFrom(phrase) {
  const found = [];
  const paren = phrase.match(/\(([A-Za-z0-9+/#]{2,})\)/g) || [];
  for (const p of paren) found.push(p.replace(/[()]/g, ""));
  return found;
}
function aliasesForSkill(skill, minLen = 1) {
  const canon = canonicalizeToken(skill) || skill.toLowerCase();
  const aliases = Object.entries(CANONICAL_ALIASES).filter(([, v]) => v === canon).map(([k]) => k);
  return Array.from(/* @__PURE__ */ new Set([canon, skill.toLowerCase(), ...aliases])).filter((a) => a.length >= minLen);
}
function parseJdRequirements(jdText) {
  const text = String(jdText || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
  const titleMatch = text.match(/Job Title:\s*(.+?)(?:\n|Mandatory Skills:|Primary Skills:|$)/i);
  const title = titleMatch?.[1]?.replace(/\s+/g, " ").trim() || "";
  const mandLines = [...text.matchAll(/Mandatory Skills:\s*([^\n]+)/gi)].map(
    (m) => splitSkillList(m[1].split("\n")[0])
  );
  const mandatoryRaw = mandLines.length === 0 ? [] : mandLines.reduce((best, line) => {
    if (line.length < 3) return best;
    if (best.length === 0) return line;
    return line.length <= best.length ? line : best;
  }, mandLines[mandLines.length - 1]);
  const requiredSet = [];
  const pushReq = (raw) => {
    const canon = canonicalizeToken(raw);
    const key = canon || raw.toLowerCase();
    if (key.length < 2) return;
    if (!requiredSet.includes(key)) requiredSet.push(key);
    for (const acr of acronymsFrom(raw)) {
      const ac = canonicalizeToken(acr);
      if (ac && !requiredSet.includes(ac)) requiredSet.push(ac);
    }
  };
  if (mandatoryRaw.length > 0) {
    for (const item of mandatoryRaw) pushReq(item);
  } else {
    const lower = normalizeProfileText(text);
    for (const phrase of PHRASE_CANONICALS) {
      if (hasPhrase(lower, phrase)) pushReq(CANONICAL_ALIASES[phrase] || phrase);
    }
    const filtered = requiredSet.filter((s) => !WEAK_BODY_SKILLS.has(s) && !TABLE_STAKES_SKILLS.has(s));
    if (filtered.length >= 2) {
      return { title, mandatoryRaw, required: filtered };
    }
  }
  return { title, mandatoryRaw, required: requiredSet };
}
function collectEmployeeSkills(employeeText) {
  const raw = normalizeProfileText(employeeText).replace(/\d+(?:\.\d+)?\s*\+?\s*years?\s+(?:of\s+)?exp(?:erience)?\s+in\s+/gi, "");
  const canonical = /* @__PURE__ */ new Set();
  for (const phrase of PHRASE_CANONICALS) {
    if (hasPhrase(raw, phrase)) canonical.add(CANONICAL_ALIASES[phrase] || phrase);
  }
  const parts = splitSkillList(raw.replace(/\n/g, ","));
  for (const part of parts) {
    const canon = canonicalizeToken(part);
    if (canon) canonical.add(canon);
    for (const acr of acronymsFrom(part)) {
      const ac = canonicalizeToken(acr);
      if (ac) canonical.add(ac);
    }
  }
  for (const [alias, canon] of Object.entries(CANONICAL_ALIASES)) {
    if (alias.length >= 3 && hasPhrase(raw, alias)) canonical.add(canon);
  }
  return { canonical, raw };
}
function employeeHasSkill(emp, required) {
  const canon = canonicalizeToken(required);
  if (emp.canonical.has(required) || emp.canonical.has(canon)) return true;
  const related = RELATED_EQUIVALENCE[canon] || RELATED_EQUIVALENCE[required] || [];
  if (related.some((r) => emp.canonical.has(r))) return true;
  if (hasPhrase(emp.raw, required) || canon && hasPhrase(emp.raw, canon)) return true;
  return false;
}
function isWeakMention(raw, skill) {
  const hedge = "basic|basics|beginner|familiar|exposure|learning|elementary";
  for (const alias of aliasesForSkill(skill, 3)) {
    const a = escapeRe(alias);
    const after = new RegExp(`\\b${a}\\b.{0,20}\\b(${hedge})\\b`, "i");
    const before = new RegExp(`\\b(${hedge})\\b.{0,24}\\b${a}\\b`, "i");
    const dashed = new RegExp(`\\b${a}\\s*[-\u2013]\\s*(${hedge})\\b`, "i");
    if (after.test(raw) || before.test(raw) || dashed.test(raw)) return true;
  }
  return false;
}
function skillCredit(emp, required) {
  if (!employeeHasSkill(emp, required)) return 0;
  if (isWeakMention(emp.raw, required)) return 0.5;
  const yearsNear = skillYearsHint(emp.raw, required);
  if (yearsNear >= 4) return 1;
  if (yearsNear >= 2) return 0.95;
  return 0.88;
}
function skillYearsHint(raw, skill) {
  for (const alias of aliasesForSkill(skill, 3)) {
    const a = escapeRe(alias);
    const m = raw.match(new RegExp(`(\\d+(?:\\.\\d+)?)\\s*\\+?\\s*years?[^.]{0,40}\\b${a}\\b`, "i")) || raw.match(new RegExp(`\\b${a}\\b[^.]{0,40}(\\d+(?:\\.\\d+)?)\\s*\\+?\\s*years?`, "i"));
    if (m) {
      const n = Number(m[1]);
      if (Number.isFinite(n)) return n;
    }
  }
  return 0;
}
function findMatchedAlias(emp, required) {
  const canon = canonicalizeToken(required) || required.toLowerCase();
  const aliases = aliasesForSkill(required, 2).sort((a, b) => b.length - a.length);
  for (const alias of aliases) {
    if (!hasPhrase(emp.raw, alias)) continue;
    if (alias.length > canon.length && (alias.includes(" ") || alias.includes(".") || alias.includes("-"))) {
      return alias.split(/[\s._-]+/).filter(Boolean).map((w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()).join(" ");
    }
    return prettySkill(canonicalizeToken(alias) || alias);
  }
  const related = RELATED_EQUIVALENCE[canon] || RELATED_EQUIVALENCE[required] || [];
  for (const r of related) {
    if (emp.canonical.has(r) || hasPhrase(emp.raw, r)) return prettySkill(r);
  }
  return "";
}
function describeSkillMatch(emp, required, credit) {
  const canon = canonicalizeToken(required) || required.toLowerCase();
  const scoring = !TABLE_STAKES_SKILLS.has(canon);
  const yearsHint = skillYearsHint(emp.raw, required);
  const years = yearsHint > 0 ? yearsHint : null;
  const foundAs = findMatchedAlias(emp, required);
  const related = RELATED_EQUIVALENCE[canon] || [];
  const viaRelated = related.find((r) => emp.canonical.has(r) || hasPhrase(emp.raw, r));
  const present = employeeHasSkill(emp, required);
  let status = "missing";
  if (credit >= 0.9) status = "strong";
  else if (credit >= 0.7) status = "solid";
  else if (credit > 0 || present) status = "weak";
  const bits = [];
  if (!present) {
    bits.push("Not found in this profile");
  } else if (isWeakMention(emp.raw, required)) {
    bits.push(foundAs ? `Only a weak mention as ${foundAs}` : "Only a weak mention");
  } else if (viaRelated && prettySkill(viaRelated).toLowerCase() !== prettySkill(canon).toLowerCase()) {
    bits.push(`Counted via ${prettySkill(viaRelated)} (treated as equivalent)`);
  } else if (foundAs && foundAs.toLowerCase() !== prettySkill(canon).toLowerCase()) {
    bits.push(`Matched as ${foundAs}`);
  } else {
    bits.push("Found in profile skills");
  }
  if (years != null) bits.push(`${years}+ years nearby in the profile`);
  if (!scoring) {
    bits.push(present ? "Table-stakes \u2014 does not change the fit score" : "Table-stakes if present \u2014 does not change the fit score");
  }
  return {
    skill: prettySkill(canon),
    status,
    credit,
    years,
    evidence: bits.join(". ") + ".",
    scoring,
    foundAs
  };
}
function prettySkill(s) {
  const special = {
    "node.js": "Node.js",
    "next.js": "Next.js",
    "ci/cd": "CI/CD",
    "c++": "C++",
    "c#": "C#",
    "sql server": "SQL Server",
    javascript: "JavaScript",
    typescript: "TypeScript",
    postgresql: "PostgreSQL",
    servicenow: "ServiceNow",
    mongodb: "MongoDB",
    mysql: "MySQL",
    aws: "AWS",
    gcp: "GCP",
    html: "HTML",
    css: "CSS",
    git: "Git",
    sql: "SQL",
    rest: "REST",
    api: "API"
  };
  if (special[s]) return special[s];
  if (s.length <= 4) return s.toUpperCase();
  return s.split(" ").map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
}
function uniquePrettySkills(raw) {
  const seen = /* @__PURE__ */ new Set();
  const unique = [];
  for (const item of raw) {
    const label = prettySkill(canonicalizeToken(item) || item.toLowerCase());
    const key = label.toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    unique.push(label);
  }
  return unique;
}
function labeledSkillValues(jdText, label) {
  const re = new RegExp(`${label}:\\s*([^\\n]+)`, "gi");
  const raw = [];
  for (const match of String(jdText || "").matchAll(re)) {
    raw.push(...splitSkillList(match[1]));
  }
  return uniquePrettySkills(raw);
}
function extractJdDisplaySkills(jdText) {
  const { mandatoryRaw, required } = parseJdRequirements(jdText);
  const labels = mandatoryRaw.length > 0 ? mandatoryRaw.map((s) => prettySkill(canonicalizeToken(s) || s.toLowerCase())) : required.map(prettySkill);
  return uniquePrettySkills(labels);
}
function extractJdMandatorySkills(jdText) {
  return labeledSkillValues(jdText, "Mandatory Skills");
}
function extractJdPrimarySkills(jdText) {
  const combined = [
    ...labeledSkillValues(jdText, "Primary Skills"),
    ...labeledSkillValues(jdText, "Secondary Skills"),
    ...labeledSkillValues(jdText, "Optional Skills")
  ];
  if (combined.length) return uniquePrettySkills(combined);
  return extractJdDisplaySkills(jdText);
}
var QUALIFIED_COVERAGE_PERCENT = 60;
function employeeSkillChips(skills) {
  const fromSkills = String(skills || "").split(/[,;|/]+/).map((skill) => skill.replace(/\s+/g, " ").trim()).filter((skill) => {
    if (skill.length < 2 || skill.length > 48) return false;
    if (skill.toLowerCase() === "none listed") return false;
    if (/^\d+\s*of\s*\d+$/i.test(skill)) return false;
    if (!/[A-Za-z]{2,}/.test(skill)) return false;
    if (/[^\x20-\x7E]/.test(skill)) return false;
    return true;
  });
  const seen = /* @__PURE__ */ new Set();
  const unique = [];
  for (const skill of fromSkills) {
    const key = skill.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(skill);
  }
  return unique;
}
function employeeMatchText(emp) {
  const skip = /* @__PURE__ */ new Set(["", "general", "employee", "beginner"]);
  const chips = employeeSkillChips(emp.skills).slice(0, 48).join(", ");
  const skillsBlob = String(emp.skills || "").replace(/\s+/g, " ").trim().slice(0, 1600);
  const experienceText = (emp.experience || []).map(
    (period) => [
      period.position,
      period.company,
      period.description,
      ...period.technologies || [],
      period.startDate,
      period.endDate || (period.current ? "Present" : "")
    ].filter(Boolean).join(" ")
  ).join(". ");
  const statedYears = emp.yearsExperience ?? emp.years_experience;
  const yearsText = statedYears == null ? "" : `${statedYears} years of experience`;
  return [chips || skillsBlob, skillsBlob && chips ? skillsBlob : "", emp.designation, emp.role, emp.grade, emp.product, yearsText, experienceText].map((v) => String(v || "").trim()).filter((v) => v && !skip.has(v.toLowerCase())).join(". ");
}
function inferJobFamily(text, title = "") {
  const blob = `${title} ${text}`.toLowerCase();
  const titleLower = title.toLowerCase();
  if (/\b(delivery manager|technical project manager|program manager|engineering manager|director|vice president|\bvp\b|head of)\b/.test(titleLower)) {
    return "manager";
  }
  if (/\b(technical lead|tech lead)\b/.test(titleLower) && !/\btest/.test(titleLower)) {
    return "engineering";
  }
  if (/\b(linux developer|linux admin|rhel)\b/.test(titleLower)) {
    return "devops";
  }
  if (/\b(devops|ansible|sre|site reliability|platform engineer|cloud engineer)\b/.test(titleLower)) {
    return "devops";
  }
  if (/\b(java developer|python developer|software engineer)\b/.test(titleLower) && !/\btest/.test(titleLower)) {
    return "backend";
  }
  if (/\b(azure admin|cloud admin)\b/.test(titleLower)) return "devops";
  if (/\b(test automation|automation testing|test engineer|sdet|qa automation|ims testing|python automation)\b/.test(
    titleLower
  )) {
    return "qa";
  }
  if (/\b(pcrf|ntas|cfx|sbc|sdm deployment|ims deployment|subscriber data)\b/.test(titleLower)) {
    return "telecom";
  }
  if (/\b(technical manager|engineering manager|program manager|delivery manager|director|vice president|\bvp\b|head of|e6|e7|e8|e9)\b/.test(blob) && !/\b(tech(?:nical)? lead|team lead)\b/.test(title.toLowerCase())) {
    if (/\b(manager|director|head of|\bvp\b)\b/.test(blob)) return "manager";
  }
  if (/\b(technical manager|engineering manager)\b/.test(blob)) return "manager";
  if (/\b(technical architect|solution architect|java architect|senior technical architect|associate principal|principal consultant)\b/.test(blob)) {
    return "manager";
  }
  const telecomHit = [...TELECOM_CORE].some((s) => hasPhrase(blob, s));
  if (telecomHit && !/\b(java developer|python developer)\b/.test(titleLower)) {
    if (/\b(test|qa|sdet)\b/.test(titleLower)) return "qa";
    return "telecom";
  }
  if (/\b(test lead|test engineer|qa\b|sdet|quality analyst|software test|qa automation|playwright|selenium|cypress|manual testing)\b/.test(blob) && !/\bkubernetes|openshift|openstack|aws|azure|devops\b/.test(blob)) {
    return "qa";
  }
  if (/\b(production support|application support|\bl1\b|\bl2\b|\bl3\b|service desk|incident manager|major incident|noc\b)\b/.test(blob)) {
    return "support";
  }
  const hasFe = [...FE_FRAMEWORKS].some((s) => hasPhrase(blob, s));
  const hasBeLang = [...BE_LANGUAGES].some((s) => hasPhrase(blob, s));
  const hasBeBeyondNode = [...BE_LANGUAGES].filter((s) => s !== "node.js" && s !== "express").some((s) => hasPhrase(blob, s));
  const hasBe = hasBeLang && hasBeBeyondNode;
  const nodeOnlyBe = hasBeLang && !hasBeBeyondNode;
  const fullstackPhrase = /\bfull[\s-]*stack\b/.test(blob);
  const frontendPhrase = /\b(frontend|front-end|ui developer|react developer)\b/.test(blob);
  const backendPhrase = /\b(backend|back-end|java developer|spring boot)\b/.test(blob);
  const aiPhrase = /\b(agentic|genai|gen ai|generative ai|langchain|langgraph|llm|machine learning)\b/.test(blob);
  const devopsPhrase = /\b(devops|ansible|sre\b|kubernetes|site reliability|terraform)\b/.test(blob);
  const icBuilder = /\b(software engineer|full[\s-]*stack|frontend|front-end|backend|developer)\b/.test(blob) && !/\b(architect|principal consultant|associate principal|technical manager|jbpm|pega)\b/.test(blob);
  if (fullstackPhrase) return "fullstack";
  if (hasFe && hasBe && icBuilder) return "fullstack";
  if (hasFe && hasBe) return "engineering";
  if (aiPhrase && !hasFe && !backendPhrase && !fullstackPhrase) return "ai";
  if (frontendPhrase || hasFe && !hasBe || hasFe && nodeOnlyBe) return "frontend";
  if (devopsPhrase && !hasFe) return "devops";
  if (backendPhrase || hasBe && !hasFe) return "backend";
  if (aiPhrase) return "ai";
  if (/\b(software engineer|developer|technical lead|tech lead)\b/.test(blob)) return "engineering";
  return "other";
}
function familyAlignment(jdFamily, personFamily) {
  if (jdFamily === personFamily) return { score: 100, relation: "match" };
  const adjacent = {
    fullstack: ["engineering", "frontend", "backend", "devops", "ai", "telecom"],
    engineering: ["fullstack", "frontend", "backend", "devops", "ai", "telecom", "qa", "support"],
    frontend: ["fullstack", "engineering", "backend"],
    backend: ["fullstack", "engineering", "devops", "ai", "telecom", "qa"],
    devops: ["backend", "engineering", "fullstack", "telecom", "qa"],
    ai: ["backend", "engineering", "fullstack"],
    telecom: ["devops", "engineering", "backend", "qa", "support"],
    qa: ["engineering", "backend", "devops", "telecom"],
    support: ["engineering", "devops", "telecom", "qa"],
    manager: ["engineering", "telecom", "devops"],
    other: ["engineering", "fullstack", "backend", "devops", "telecom"]
  };
  const mismatchPairs = [
    ["manager", "frontend"]
  ];
  if (mismatchPairs.some(([a, b]) => jdFamily === a && personFamily === b || jdFamily === b && personFamily === a)) {
    return { score: 42, relation: "mismatch" };
  }
  if ((adjacent[jdFamily] || []).includes(personFamily)) {
    const score = jdFamily === "backend" && personFamily === "engineering" ? 92 : jdFamily === "engineering" && personFamily === "backend" ? 92 : jdFamily === "fullstack" && personFamily === "backend" ? 80 : jdFamily === "fullstack" && personFamily === "frontend" ? 78 : jdFamily === "fullstack" && personFamily === "engineering" ? 86 : jdFamily === "devops" && personFamily === "telecom" ? 78 : jdFamily === "telecom" && personFamily === "devops" ? 76 : jdFamily === "devops" && (personFamily === "backend" || personFamily === "engineering") ? 56 : jdFamily === "qa" && (personFamily === "backend" || personFamily === "engineering" || personFamily === "devops") ? 56 : jdFamily === "telecom" && (personFamily === "backend" || personFamily === "engineering") ? 70 : 72;
    return { score, relation: "adjacent" };
  }
  return { score: 55, relation: "adjacent" };
}
function parseYears(text) {
  const m = String(text || "").match(/(\d+(?:\.\d+)?)\s*\+?\s*years?\s+of\s+exp/i) || String(text || "").match(/(\d+(?:\.\d+)?)\s*\+?\s*years?\b/i);
  if (!m) return null;
  const n = Number(m[1]);
  return Number.isFinite(n) ? n : null;
}
function experienceMonth(value) {
  const text = String(value || "").trim();
  if (!text) return null;
  if (/\b(present|current|now)\b/i.test(text)) {
    const now = /* @__PURE__ */ new Date();
    return now.getFullYear() * 12 + now.getMonth();
  }
  const yearFirst = text.match(/\b((?:19|20)\d{2})[-/](\d{1,2})\b/);
  if (yearFirst) {
    const month = Number(yearFirst[2]);
    return month >= 1 && month <= 12 ? Number(yearFirst[1]) * 12 + month - 1 : null;
  }
  const monthFirst = text.match(/\b(\d{1,2})[-/]((?:19|20)\d{2})\b/);
  if (monthFirst) {
    const month = Number(monthFirst[1]);
    return month >= 1 && month <= 12 ? Number(monthFirst[2]) * 12 + month - 1 : null;
  }
  const namedMonth = text.match(/\b([A-Za-z]{3,9})[,. ]+((?:19|20)\d{2})\b/);
  if (namedMonth) {
    const months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
    const month = months.indexOf(namedMonth[1].slice(0, 3).toLowerCase());
    if (month >= 0) return Number(namedMonth[2]) * 12 + month;
  }
  const yearOnly = text.match(/\b((?:19|20)\d{2})\b/);
  return yearOnly ? Number(yearOnly[1]) * 12 : null;
}
function profileExperienceYears(periods, explicitYears, profileText) {
  if (typeof explicitYears === "number" && Number.isFinite(explicitYears) && explicitYears >= 0) {
    return explicitYears;
  }
  if (typeof explicitYears === "string") {
    const numeric = explicitYears.trim().match(/^(\d+(?:\.\d+)?)$/);
    const stated = numeric || explicitYears.match(/(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b/i);
    if (stated) return Number(stated[1]);
  }
  const intervals = (periods || []).map((period) => {
    const start = experienceMonth(period.startDate);
    const end = experienceMonth(period.endDate) ?? (period.current ? experienceMonth("Present") : null);
    return start != null && end != null && end >= start ? [start, end] : null;
  }).filter((interval) => interval !== null).sort((a, b) => a[0] - b[0]);
  if (intervals.length) {
    let totalMonths = 0;
    let [start, end] = intervals[0];
    for (const [nextStart, nextEnd] of intervals.slice(1)) {
      if (nextStart <= end + 1) {
        end = Math.max(end, nextEnd);
      } else {
        totalMonths += end - start;
        start = nextStart;
        end = nextEnd;
      }
    }
    totalMonths += end - start;
    return Math.floor(totalMonths / 12 * 10) / 10;
  }
  return parseYears(profileText);
}
function extractJdRequiredExperienceYears(jdText) {
  const text = String(jdText || "");
  const patterns = [
    /\b(?:minimum|min\.?|at least)\s*(?:of\s*)?(\d+(?:\.\d+)?)\s*(?:\+|plus)?\s*(?:years?|yrs?)\b/gi,
    /\b(\d+(?:\.\d+)?)\s*(?:\+|plus|[-–—]\s*\d+(?:\.\d+)?)?\s*(?:years?|yrs?)\s*(?:of\s+)?(?:(?:relevant|professional|work|overall|total)\s+)?experience\b/gi,
    /\b(?:experience|exp)\s*(?:(?:required|minimum|min)\s*)?(?:of|:|=)?\s*(\d+(?:\.\d+)?)\s*(?:\+|plus|[-–—]\s*\d+(?:\.\d+)?)?\s*(?:years?|yrs?)\b/gi
  ];
  const values = patterns.flatMap(
    (pattern) => [...text.matchAll(pattern)].map((match) => Number(match[1])).filter(Number.isFinite)
  );
  return values.length ? Math.max(...values) : null;
}
function parseGrade(text) {
  const m = String(text || "").match(/\be([1-9])\b/i);
  return m ? Number(m[1]) : null;
}
function jdIsIcBuilder(title) {
  const t = title.toLowerCase();
  if (/\b(manager|director|head|vp)\b/.test(t)) return false;
  return /\b(engineer|developer|programmer|analyst)\b/.test(t) && !/\b(test engineer|support engineer)\b/.test(t) || /\bfull[\s-]*stack\b/.test(t);
}
function levelFit(profileText, jdTitle, personFamily) {
  const grade = parseGrade(profileText);
  const years = parseYears(profileText);
  const ic = jdIsIcBuilder(jdTitle);
  if (personFamily === "manager" && ic) return 55;
  if (personFamily === "qa" && ic) return 62;
  let score = 88;
  if (ic) {
    if (grade != null) {
      if (grade <= 3) score = 95;
      else if (grade === 4) score = 88;
      else if (grade === 5) score = 82;
      else score = 70;
    }
    if (/\b(senior technical lead|technical lead|tech lead)\b/.test(profileText) && !/\blead\b/.test(jdTitle.toLowerCase())) {
      score = Math.min(score, 84);
    }
  }
  if (years != null && years < 3 && ic) score = Math.min(score, 70);
  if (years != null && years >= 12 && ic && !/\b(lead|senior|principal|staff)\b/.test(jdTitle.toLowerCase())) {
    score = Math.min(score, 80);
  }
  return score;
}
function stackFit(jdFamily, required, credits, coreCoverage = 0) {
  if (jdFamily === "fullstack") {
    const reqFe = required.filter((s) => FE_FRAMEWORKS.has(s));
    const reqBe = required.filter((s) => BE_LANGUAGES.has(s));
    const hasFe = reqFe.length === 0 || reqFe.some((s) => (credits.get(s) || 0) >= 0.7);
    const hasBe = reqBe.length === 0 || reqBe.some((s) => (credits.get(s) || 0) >= 0.7);
    if (hasFe && hasBe) return 100;
    if (hasFe || hasBe) return 50;
    return 15;
  }
  return Math.round(55 + 45 * Math.max(0, Math.min(1, coreCoverage)));
}
function pickCoreSkills(title, required) {
  if (required.length <= 4) return { core: required, extra: [] };
  const titleBlob = title.toLowerCase();
  const ranked = required.map((skill, index) => {
    const canon = canonicalizeToken(skill) || skill;
    let weight = Math.max(0, 6 - index);
    if (hasPhrase(titleBlob, canon) || hasPhrase(titleBlob, skill)) weight += 24;
    if (BE_LANGUAGES.has(canon) || FE_FRAMEWORKS.has(canon)) weight += 14;
    const titleIsAppDev = /\b(java|python|\.net|react|angular)\b/.test(titleBlob);
    const titleIsTelecom = /\b(pcrf|sdm|ims|ntas|cfx|sbc|hss|hlr)\b/.test(titleBlob);
    const titleIsAzure = /\bazure\b/.test(titleBlob);
    const titleIsDevops = /\b(devops|ansible|sre|site reliability)\b/.test(titleBlob);
    const titleIsLinux = /\b(linux|rhel)\b/.test(titleBlob);
    const titleIsQa = /\b(test|qa|sdet|automation|playwright|selenium)\b/.test(titleBlob);
    if (CLOUD_CORE.has(canon)) {
      weight += titleIsTelecom ? 6 : titleIsLinux || titleIsQa ? -8 : 12;
    }
    if (TELECOM_CORE.has(canon)) weight += titleIsAppDev ? -10 : 14;
    if (titleIsAzure && ["kubernetes", "linux", "helm", "azure", "openshift"].includes(canon)) weight += 16;
    if (titleIsAzure && canon === "openstack") weight -= 8;
    if (titleIsDevops && DEVOPS_CORE.has(canon)) weight += 32;
    if (titleIsDevops && DEVOPS_NOISE.has(canon)) weight -= 28;
    if (titleIsDevops && canon === "python") weight -= 10;
    if (titleIsLinux && LINUX_CORE.has(canon)) weight += 32;
    if (titleIsLinux && LINUX_NOISE.has(canon)) weight -= 28;
    if (titleIsQa && QA_CORE.has(canon)) weight += 32;
    if (titleIsQa && QA_NOISE.has(canon)) weight -= 28;
    if (titleIsQa && canon === "python") weight += 12;
    if (TABLE_STAKES_SKILLS.has(canon) || WEAK_BODY_SKILLS.has(canon)) weight -= 20;
    return { skill: canon, weight };
  });
  ranked.sort((a, b) => b.weight - a.weight || a.skill.localeCompare(b.skill));
  const coreCount = Math.min(4, Math.max(3, Math.round(required.length * 0.3)));
  const core = [];
  const seen = /* @__PURE__ */ new Set();
  for (const row of ranked) {
    if (seen.has(row.skill)) continue;
    seen.add(row.skill);
    core.push(row.skill);
    if (core.length >= coreCount) break;
  }
  const extra = required.filter((skill) => !seen.has(canonicalizeToken(skill) || skill));
  return { core, extra };
}
function decide(score, relation, coveragePct, stack, jdFamily) {
  if (score >= 75 && coveragePct >= 50) return "interview";
  if (score >= QUALIFIED_COVERAGE_PERCENT) return "screen";
  if (score >= 30) return "hold";
  return "reject";
}
function compileJdForMatch(jdSkills) {
  const text = String(jdSkills || "").trim();
  if (!text) {
    return {
      jdText: "",
      parsed: { title: "", mandatoryRaw: [], required: [] },
      required: [],
      scoredSkills: [],
      core: [],
      extra: [],
      jdFamily: "other",
      emptyReason: "No skills or requirement text to score."
    };
  }
  const parsed = parseJdRequirements(text);
  const labeled = [...extractJdMandatorySkills(text), ...extractJdPrimarySkills(text)];
  const required = [];
  const pushReq = (raw) => {
    const canon = canonicalizeToken(raw) || String(raw || "").toLowerCase().trim();
    if (canon.length < 2 || TABLE_STAKES_SKILLS.has(canon)) return;
    if (!required.includes(canon)) required.push(canon);
  };
  for (const item of parsed.required) pushReq(item);
  for (const item of labeled) pushReq(item);
  if (required.length === 0) {
    return {
      jdText: text,
      parsed,
      required: [],
      scoredSkills: [],
      core: [],
      extra: [],
      jdFamily: inferJobFamily(text, parsed.title),
      emptyReason: "No required JD skills to score against."
    };
  }
  const scoringRequired = required.filter((s) => !TABLE_STAKES_SKILLS.has(s));
  const scoredSkills = scoringRequired.length > 0 ? scoringRequired : required;
  const { core, extra } = pickCoreSkills(
    `${parsed.title} ${parsed.required.join(" ")}`.trim(),
    scoredSkills
  );
  return {
    jdText: text,
    parsed,
    required,
    scoredSkills,
    core,
    extra,
    jdFamily: inferJobFamily(text, parsed.title)
  };
}
function calculateSkillMatch(employeeSkills, jdSkills, compiled, verifiedExperienceYears) {
  if (!employeeSkills?.trim() || !jdSkills?.trim()) {
    return emptyMatch();
  }
  const jd = compiled && compiled.jdText === String(jdSkills || "").trim() ? compiled : compileJdForMatch(jdSkills);
  if (jd.emptyReason) return emptyMatch(jd.emptyReason);
  const { parsed, required, scoredSkills, core, extra, jdFamily } = jd;
  const chips = employeeSkillChips(employeeSkills).slice(0, 48);
  const chipBlob = chips.join(", ");
  const header = employeeSkills.split(/\n/).slice(0, 10).join(" ").slice(0, 900);
  const profileBlob = [chipBlob, header].filter(Boolean).join(". ").slice(0, 2200);
  const emp = collectEmployeeSkills(profileBlob);
  const personFamily = inferJobFamily(profileBlob, header || chipBlob);
  let alignment = familyAlignment(jdFamily, personFamily);
  if (isTechnicalFamily(jdFamily) && !isTechnicalFamily(personFamily)) {
    alignment = { score: Math.min(alignment.score, 28), relation: "mismatch" };
  } else if (!isTechnicalFamily(jdFamily) && isTechnicalFamily(personFamily)) {
    alignment = {
      score: Math.min(alignment.score, 48),
      relation: alignment.relation === "match" ? "adjacent" : alignment.relation
    };
  }
  const credits = /* @__PURE__ */ new Map();
  const matchedFull = [];
  for (const req of scoredSkills) {
    const credit = skillCredit(emp, req);
    credits.set(req, credit);
    if (credit >= 0.7) matchedFull.push(req);
  }
  const avgCredit = (skills) => skills.length ? skills.reduce((sum, skill) => sum + (credits.get(skill) || 0), 0) / skills.length : 0;
  const techSkills = scoredSkills.filter((s) => !isNonTechnicalSkill(s));
  const nonTechSkills = scoredSkills.filter((s) => isNonTechnicalSkill(s));
  const techCore = core.filter((s) => !isNonTechnicalSkill(s));
  const coreCoverage = avgCredit(techCore.length ? techCore : core);
  const extraTech = extra.filter((s) => !isNonTechnicalSkill(s));
  const extraCoverage = extraTech.length ? avgCredit(extraTech) : coreCoverage;
  const coveragePct = (0.82 * coreCoverage + 0.18 * extraCoverage) * 100;
  const nonTechPct = nonTechSkills.length ? avgCredit(nonTechSkills) * 100 : 70;
  const nonTechUsed = coveragePct < 40 ? Math.max(20, nonTechPct) * 0.35 : Math.max(55, nonTechPct);
  const stack = Math.round(nonTechUsed);
  const level = levelFit(emp.raw, parsed.title, personFamily);
  const fullstackSides = stackFit(jdFamily, scoredSkills, credits, coreCoverage);
  let score = Math.round(
    0.5 * coveragePct + 0.25 * alignment.score + 0.15 * nonTechUsed + 0.1 * level
  );
  const years = verifiedExperienceYears ?? parseYears(emp.raw);
  const mandatorySkills = extractJdMandatorySkills(jd.jdText);
  const missingMandatorySkills = mandatorySkills.filter(
    (skill) => skillCredit(emp, canonicalizeToken(skill) || skill) < 0.7
  );
  const requiredExperienceYears = extractJdRequiredExperienceYears(jd.jdText);
  const experienceNotVerified = requiredExperienceYears != null && (years == null || years < requiredExperienceYears);
  if (jdFamily === "fullstack" && fullstackSides < 100) {
    score = Math.min(score, 72);
    if (years != null && years < 3) score = Math.min(score, 58);
  }
  if (isTechnicalFamily(jdFamily) && coveragePct < 35) {
    score = Math.min(score, 52);
  }
  const coreTech = (techCore.length ? techCore : techSkills).slice(0, 4);
  const headEmp = collectEmployeeSkills(chips.slice(0, 3).join(", "));
  const headTechPct = coreTech.length ? coreTech.reduce((sum, skill) => sum + skillCredit(headEmp, skill), 0) / coreTech.length * 100 : 0;
  if (isTechnicalFamily(jdFamily) && headTechPct < 15) {
    score = Math.min(score, 54);
  }
  const titleCritical = techSkills.filter((skill) => {
    const canon = canonicalizeToken(skill) || skill;
    if (hasPhrase(parsed.title.toLowerCase(), canon) || hasPhrase(parsed.title.toLowerCase(), skill)) {
      return true;
    }
    return jdFamily === "devops" && (DEVOPS_CORE.has(canon) || LINUX_CORE.has(canon)) || jdFamily === "qa" && QA_CORE.has(canon);
  });
  const titleCoverage = titleCritical.length ? avgCredit(titleCritical) : 1;
  if (titleCritical.length > 0 && titleCoverage < 0.35) {
    score = Math.min(score, 48);
  }
  if ((jdFamily === "devops" || jdFamily === "qa") && titleCoverage < 0.45) {
    score = Math.min(score, 50);
  }
  if (missingMandatorySkills.length > 0 || experienceNotVerified) {
    score = Math.min(score, QUALIFIED_COVERAGE_PERCENT - 1);
  }
  score = Math.max(0, Math.min(100, score));
  const matchingSkills = [];
  const seenMatch = /* @__PURE__ */ new Set();
  for (const label of matchedFull.map(prettySkill)) {
    const key = label.toLowerCase();
    if (!key || seenMatch.has(key)) continue;
    seenMatch.add(key);
    matchingSkills.push(label);
  }
  const decision = decide(score, alignment.relation, coveragePct, stack, jdFamily);
  const missingCore = scoredSkills.filter((s) => (credits.get(s) || 0) < 0.7).map(prettySkill);
  const rationaleParts = [
    `${decision === "screen" ? "Screen" : decision === "interview" ? "Interview" : decision === "hold" ? "Hold" : "Reject"} as ${personFamily} for a ${jdFamily} req`,
    matchingSkills.length ? `technical hits: ${matchingSkills.join(", ")}` : "no solid technical hits"
  ];
  if (missingCore.length) rationaleParts.push(`missing or weak: ${missingCore.join(", ")}`);
  if (missingMandatorySkills.length) {
    rationaleParts.push(`mandatory skills not verified: ${missingMandatorySkills.map(prettySkill).join(", ")}`);
  }
  if (experienceNotVerified && requiredExperienceYears != null) {
    rationaleParts.push(
      years == null ? `minimum ${requiredExperienceYears} years of experience not verified in the profile` : `minimum ${requiredExperienceYears} years required; profile verifies ${years} years`
    );
  }
  if (alignment.relation === "mismatch") {
    rationaleParts.push(
      isTechnicalFamily(jdFamily) && !isTechnicalFamily(personFamily) ? "non-technical profile on a technical requirement" : "job family does not match this requirement"
    );
  }
  const breakdownSeen = /* @__PURE__ */ new Set();
  const skillBreakdown = [];
  const orderedReq = [
    ...scoredSkills,
    ...required.filter((s) => !scoredSkills.includes(s))
  ];
  for (const req of orderedReq) {
    const key = (canonicalizeToken(req) || req).toLowerCase();
    if (!key || breakdownSeen.has(key)) continue;
    breakdownSeen.add(key);
    const credit = credits.has(req) ? credits.get(req) || 0 : skillCredit(emp, req);
    skillBreakdown.push(describeSkillMatch(emp, req, credit));
  }
  const requiredCanon = new Set(required.map((s) => canonicalizeToken(s) || s));
  const knownSkills = new Set(Object.values(CANONICAL_ALIASES));
  const bonusSkills = [];
  const bonusSeen = /* @__PURE__ */ new Set();
  for (const skill of emp.canonical) {
    if (!knownSkills.has(skill)) continue;
    if (requiredCanon.has(skill) || TABLE_STAKES_SKILLS.has(skill)) continue;
    const label = prettySkill(skill);
    const k = label.toLowerCase();
    if (!k || bonusSeen.has(k)) continue;
    bonusSeen.add(k);
    bonusSkills.push(label);
  }
  bonusSkills.sort((a, b) => a.localeCompare(b));
  const scoreParts = {
    coveragePct: Math.round(coveragePct),
    familyScore: alignment.score,
    stackScore: stack,
    levelScore: level,
    years,
    grade: parseGrade(emp.raw),
    weighted: {
      coverage: Math.round(0.5 * coveragePct),
      family: Math.round(0.25 * alignment.score),
      stack: Math.round(0.15 * stack),
      level: Math.round(0.1 * level)
    }
  };
  return {
    score,
    strictRequirementsPassed: missingMandatorySkills.length === 0 && !experienceNotVerified,
    matchingSkills,
    matchedCount: matchedFull.length,
    requiredCount: scoredSkills.length,
    decision,
    rationale: `${rationaleParts.join(". ")}.`,
    familyRelation: alignment.relation,
    personFamily,
    jdFamily,
    skillBreakdown,
    bonusSkills: bonusSkills.slice(0, 16),
    scoreParts
  };
}

// ../python/vendor/skill_match_entry.ts
function readStdin() {
  return new Promise((resolve) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => data += chunk);
    process.stdin.on("end", () => resolve(data));
  });
}
async function main() {
  const input = JSON.parse(await readStdin());
  const compiled = compileJdForMatch(input.jdText);
  const results = input.people.map((person) => {
    const profileText = employeeMatchText(person);
    const years = profileExperienceYears(person.experience, person.yearsExperience, profileText);
    const match = calculateSkillMatch(profileText, input.jdText, compiled, years);
    return {
      score: match.score,
      strictRequirementsPassed: match.strictRequirementsPassed,
      decision: match.decision,
      rationale: match.rationale,
      matchingSkills: match.matchingSkills
    };
  });
  process.stdout.write(JSON.stringify(results));
}
main();
