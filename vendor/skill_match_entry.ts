import {
  calculateSkillMatch,
  compileJdForMatch,
  employeeMatchText,
  profileExperienceYears,
  type ExperiencePeriod,
} from "../../UI/src/lib/skill-match";

type Person = {
  skills?: string;
  designation?: string;
  grade?: string;
  product?: string;
  role?: string;
  experience?: ExperiencePeriod[];
  yearsExperience?: number | string | null;
};

function readStdin(): Promise<string> {
  return new Promise((resolve) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => (data += chunk));
    process.stdin.on("end", () => resolve(data));
  });
}

async function main() {
  const input = JSON.parse(await readStdin()) as { jdText: string; people: Person[] };
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
      matchingSkills: match.matchingSkills,
    };
  });
  process.stdout.write(JSON.stringify(results));
}

main();
