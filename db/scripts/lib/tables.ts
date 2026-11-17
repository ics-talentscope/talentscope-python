import { join } from "path";

export const ROOT = process.cwd();
export const MIGRATIONS_DIR = join(ROOT, "mssql", "migrations");
export const DATA_DIR = join(ROOT, "mssql", "data");

export type TableDef = {
  name: string;
  /** Primary key column(s); rows are matched on these when deploying. */
  pk: string[];
};

/**
 * Every table whose data lives in mssql/data/<name>.json.
 * Order matters: parents before children (foreign keys). Deletes run in reverse.
 */
export const TABLES: TableDef[] = [
  { name: "employees", pk: ["id"] },
  { name: "employee_portal_question_bank", pk: ["id"] },
  { name: "resumes", pk: ["id"] },
  { name: "job_descriptions", pk: ["id"] },
  { name: "portal_settings", pk: ["key"] },
  { name: "audit_logs", pk: ["id"] },
  { name: "simulated_emails", pk: ["id"] },
  { name: "candidate_interview_data", pk: ["id"] },
  { name: "interview_questions", pk: ["id"] },
  { name: "interview_attempts", pk: ["id"] },
  { name: "candidate_sessions", pk: ["id"] },
  { name: "reset_logs", pk: ["id"] },
  { name: "learning_subjects", pk: ["id"] },
  { name: "learning_modules", pk: ["id"] },
  { name: "learning_topics", pk: ["id"] },
  { name: "learning_resources", pk: ["id"] },
  { name: "tests", pk: ["id"] },
  { name: "test_questions", pk: ["id"] },
  { name: "test_attempts", pk: ["id"] },
  { name: "evaluations", pk: ["id"] },
  { name: "behavior_evaluations", pk: ["id"] },
  { name: "business_impacts", pk: ["id"] },
];

export function dataFile(table: string): string {
  return join(DATA_DIR, `${table}.json`);
}

export function selectTables(filter: string | null): TableDef[] {
  if (!filter) return TABLES;
  const wanted = filter
    .split(",")
    .map((t) => t.trim().toLowerCase())
    .filter(Boolean);
  for (const w of wanted) {
    if (!TABLES.some((t) => t.name === w)) {
      throw new Error(`Unknown table "${w}". Known: ${TABLES.map((t) => t.name).join(", ")}`);
    }
  }
  return TABLES.filter((t) => wanted.includes(t.name));
}

export function quoteIdent(name: string): string {
  return `[${name.replace(/]/g, "]]")}]`;
}
