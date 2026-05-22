"""Engineering competency leveling data.

Source: "Engineering Competency Leveling Guide" (FBG 2025).

The deck defines 10 numeric levels. The career-ladder titles we care about on
the Fantasy team map to the subset below. Level definitions are cumulative —
the deck states explicitly that "the competency definitions for each level
are inclusive of the definitions for the preceding levels," so when we render
a card we also surface the next level to make promotion paths tangible.
"""

from __future__ import annotations

from typing import Dict, List, Optional


# Map the career-ladder titles we use in team_config.yaml to the deck's level
# numbers. If the title isn't here, we just won't show a competency button.
TITLE_TO_LEVEL: Dict[str, int] = {
    "Engineer I":        1,
    "Engineer II":       2,
    "Engineer III":      3,
    "Senior Engineer":   4,
    "Staff Engineer":    5,
    "Senior Staff Engineer": 6,
    "Principal Engineer":    7,
    "Senior Principal Engineer": 8,
    "Distinguished Engineer":    9,
    "Senior VP Engineering":    10,
}

# Human-friendly label for each level (what we show on screen).
LEVEL_LABELS: Dict[int, str] = {
    1:  "Level 1 · Engineer I",
    2:  "Level 2 · Engineer II",
    3:  "Level 3 · Engineer III",
    4:  "Level 4 · Senior Engineer",
    5:  "Level 5 · Staff Engineer",
    6:  "Level 6 · Senior Staff Engineer",
    7:  "Level 7 · Principal Engineer",
    8:  "Level 8 · Senior Principal Engineer",
    9:  "Level 9 · Distinguished Engineer",
    10: "Level 10 · Senior VP Engineering",
}


# Per-level competency content. Keys mirror the deck's categorization.
# "who_you_are" is from the "WHO YOU ARE" box.
# "results" combines EXPERTISE and SCOPE.
# "behaviors" combines COLLABORATION, OWNERSHIP, AUTONOMY, COMMUNICATION, PROBLEM SOLVING.
# "leadership" is the standalone LEADERSHIP box.
COMPETENCIES: Dict[int, Dict[str, List[Dict[str, str]] | str]] = {
    1: {
        "who_you_are": "Intern or entry-level support role. Individual contributor.",
        "results": [
            {"label": "Expertise", "text": "You deliver timely and quality work that is more task oriented with direction from manager or peers."},
            {"label": "Scope", "text": "Your work supports team goals."},
        ],
        "behaviors": [
            {"label": "Collaboration", "text": "You are collaborative, inclusive and helpful to your peers and are receptive to feedback."},
            {"label": "Ownership", "text": "You embrace accountability and responsibility for your work."},
            {"label": "Autonomy", "text": "You are able to prioritize work with direction from manager or peers."},
            {"label": "Communication", "text": "You demonstrate active listening and professional verbal and written communication skills."},
            {"label": "Problem Solving", "text": "You can articulate problems and may be able to suggest possible solutions."},
        ],
        "leadership": "Demonstrates accountability and reliability in completing assigned work. Listens to direction from others and learns from feedback. Contributes positively to team goals by being dependable and collaborative.",
    },
    2: {
        "who_you_are": "Operational or support role. Individual contributor (mid).",
        "results": [
            {"label": "Expertise", "text": "You deliver timely and quality work that includes fundamental tasks that can be executed with less direction."},
            {"label": "Scope", "text": "Your work supports team goals."},
        ],
        "behaviors": [
            {"label": "Collaboration", "text": "You are collaborative, inclusive and build on the ideas of others."},
            {"label": "Ownership", "text": "You take initiative and responsibility for your work and you display a growth mindset when facing challenges."},
            {"label": "Autonomy", "text": "You collaborate and prioritize on what work is most important with your manager. Operates independently on simple tasks and contributes reliably to team goals."},
            {"label": "Communication", "text": "You communicate concisely and clearly."},
            {"label": "Problem Solving", "text": "You can articulate basic problems and explore solutions. You use your judgment to effectively resolve routine and non-routine issues."},
        ],
        "leadership": "Takes ownership of personal tasks and supports peers when possible. Shares learnings and communicates effectively with teammates. Models positive behaviors and helps create a supportive work environment.",
    },
    3: {
        "who_you_are": "Experienced operational or support role. Individual contributor (mid).",
        "results": [
            {"label": "Expertise", "text": "You deliver and contribute to more complex end-to-end projects with less direction."},
            {"label": "Scope", "text": "Your work primarily supports team and occasionally larger departmental goals."},
        ],
        "behaviors": [
            {"label": "Collaboration", "text": "You have effective working relationships with team members and begin to demonstrate strong cross-functional partnerships. You are collaborative, inclusive and helpful to your peers."},
            {"label": "Ownership", "text": "You own your mistakes, you’re receptive to feedback, and take responsibility for your behavior. You support continuous improvements for your direct work."},
            {"label": "Autonomy", "text": "You give input and make suggestions on prioritization of work with your manager. Operates independently on more complex tasks and contributes reliably to team goals."},
            {"label": "Communication", "text": "You seek to understand other points of view within your team."},
            {"label": "Problem Solving", "text": "You identify and articulate more complex problems and propose solutions."},
        ],
        "leadership": "Begins influencing outcomes by supporting and guiding peers. Proactively identifies opportunities for team improvement. Communicates with clarity and helps others understand goals and expectations.",
    },
    4: {
        "who_you_are": "Senior operational or support role; entry to mid-level in a technical or professional discipline. Individual contributor (experienced).",
        "results": [
            {"label": "Expertise", "text": "You solve difficult problems, applying appropriate technologies and best practices. You are proficient in a broad range of design approaches and know when it is appropriate to use them and when it is not."},
            {"label": "Scope", "text": "Your work primarily supports team and often larger departmental goals."},
        ],
        "behaviors": [
            {"label": "Collaboration", "text": "You are collaborative, inclusive and helpful to your peers & lower levels."},
            {"label": "Ownership", "text": "You do things with the proper level of complexity the first time."},
            {"label": "Autonomy", "text": "Your manager defines the strategy, but you are responsible for designing a solution."},
            {"label": "Communication", "text": "You are able advise others through effective communication and relationships."},
            {"label": "Problem Solving", "text": "You work with your team to invent, design, and build solutions."},
        ],
        "leadership": "Demonstrates consistent ownership and accountability across workstreams. Mentors and supports others to build capability and confidence. Promotes collaboration and models company values through everyday actions.",
    },
    5: {
        "who_you_are": "Staff or Manager as an individual contributor or people manager.",
        "results": [
            {"label": "Expertise", "text": "You deliver and contribute to complex problems, sometimes cross-functional, which you execute with minimal guidance."},
            {"label": "Scope", "text": "Your work supports team and department goals."},
        ],
        "behaviors": [
            {"label": "Collaboration", "text": "You’re considered by peers and leaders to be an effective partner in listening. You integrate and encourage the expression of opposing views. You proactively request and provide effective feedback to your peers and manager. You effectively engage internal and external stakeholders."},
            {"label": "Ownership", "text": "You assume accountability for the quality of your work and shared deliverables."},
            {"label": "Autonomy", "text": "You make suggestions on prioritization, which is then approved by manager. Operates with growing independence, yet still relies on guidance for prioritization or complex decision-making."},
            {"label": "Communication", "text": "You communicate complex problems, goals, and ideas clearly."},
            {"label": "Problem Solving", "text": "You articulate the root cause of problems, then extract and translate insights into solutions."},
        ],
        "leadership": "Exemplifies company tenets in daily work and leads by example. Supports others’ growth through mentorship, knowledge sharing, and collaboration. Seeks opportunities for teammates to learn and develop. Actively contributes to an inclusive, welcoming environment. Takes ownership of outcomes and encourages accountability within the team. Achieves goals with guidance from manager and PBPs.",
    },
    6: {
        "who_you_are": "Sr. Staff or Sr. Manager as an individual contributor or people manager.",
        "results": [
            {"label": "Expertise", "text": "You identify new strategic opportunities and lead projects & programs that positively impact organizational performance with minimal guidance."},
            {"label": "Scope", "text": "Your work supports departmental goals."},
        ],
        "behaviors": [
            {"label": "Collaboration", "text": "You consistently deliver effective feedback to your peers and manager in a way that strengthens relationships and enables projects to advance faster. You effectively address and resolve basic conflicts within your scope."},
            {"label": "Ownership", "text": "You own the outcomes of your projects and its impact on departmental objectives."},
            {"label": "Autonomy", "text": "You establish your own goals and priorities in collaboration with your manager and make necessary adjustments to meet team/company goals. Operates with minimal oversight; trusted to make complex decisions and shape how work gets done."},
            {"label": "Communication", "text": "You develop compelling narratives and present complex problems and goals verbally & in written form."},
            {"label": "Problem Solving", "text": "You have a solutions-first approach and anticipate issues and blockers proactively. You seek strategic partnership and input from peers and stakeholders."},
        ],
        "leadership": "Models company tenets and encourages others to do the same. Mentors and coaches others to strengthen their skills and confidence. Creates learning and growth opportunities across teams. Fosters an inclusive, respectful environment where diverse perspectives are valued. Empowers others through collaboration and shared ownership. Drives performance and results with minimal guidance, demonstrating strong personal leadership.",
    },
    7: {
        "who_you_are": "Principal or Director role as an individual contributor or people manager.",
        "results": [
            {"label": "Expertise", "text": "You lead, contribute and execute cross-functional, high impact and future-facing strategy work independently."},
            {"label": "Scope", "text": "Your work supports your department’s goals and you are asked to provide input into company-level goals."},
        ],
        "behaviors": [
            {"label": "Collaboration", "text": "You consistently deliver constructive and timely feedback to people at all levels in a way that strengthens relationships and enables functional initiatives to advance more quickly."},
            {"label": "Ownership", "text": "You own and initiate the strategy and execution of your work/team and their impact on departmental & company goals."},
            {"label": "Autonomy", "text": "You prioritize in partnership with senior leaders and help shape prioritization for lower levels. Executes independently and is accountable for end-to-end outcomes. Operates as a trusted expert or leader."},
            {"label": "Communication", "text": "You improve how your team communicates by defining processes, standards, and best practices. Not afraid to challenge others when appropriate."},
            {"label": "Problem Solving", "text": "You proactively analyze and surface meaningful patterns and trends that reveal underlying root causes and inform strategic decisions."},
        ],
        "leadership": "Champions company tenets and holds self and others accountable to them. Develops and mentors talent across teams or functions. Creates and sustains opportunities for learning, growth, and inclusion. Empowers others through influence, strategic collaboration, and delegation. Leads through influence across teams or initiatives to achieve successful outcomes with minimal guidance.",
    },
    8: {
        "who_you_are": "Sr. Principal or Sr. Director commonly in a people manager role.",
        "results": [
            {"label": "Expertise", "text": "You use your in-depth knowledge to anticipate complex issues with high impact on the company and business trajectory before they occur."},
            {"label": "Scope", "text": "Your work supports departmental and company level goals. You lead complex projects that require cross-functional alignment and collaboration."},
        ],
        "behaviors": [
            {"label": "Collaboration", "text": "You communicate and work transparently with other departments and encourages peers to do the same."},
            {"label": "Ownership", "text": "You lead complex projects that require cross-functional alignment and collaboration."},
            {"label": "Autonomy", "text": "Operates with broad autonomy, seeking input for alignment with senior leaders but not direction. Anticipates risks and makes judgment calls with limited review. Escalates only when decisions have enterprise-level impact."},
            {"label": "Communication", "text": "You set an example for tone and communication standards within your function and across the company."},
            {"label": "Problem Solving", "text": "You lead through change and ambiguity. Anticipate and consider risks while shaping ongoing strategy."},
        ],
        "leadership": "You model and exemplify company tenets and hold others accountable to them. You develop and mentor your team members, and coach your leaders to do the same. You hire and invest in the right people to build effective teams. You hold your teams accountable to an inclusive environment that’s welcoming to all. You delegate to empower team members. You develop others through effective stretch assignments, projects and initiatives. You manage performance of teams to successful outcomes with little guidance. You challenge the status quo and encourage differing viewpoints within your team and across the organization.",
    },
    9: {
        "who_you_are": "Distinguished or VP commonly in a people manager role.",
        "results": [
            {"label": "Expertise", "text": "You are focused on setting the long-term strategic direction for the company, while rallying everyone towards a common vision."},
            {"label": "Scope", "text": "Your work supports company level goals. You lead multi-disciplined teams to deliver organizational objectives."},
        ],
        "behaviors": [
            {"label": "Collaboration", "text": "You encourage harmony amidst disparate perspectives and navigate conflict when it arises while empowering team members to resolve conflict independently."},
            {"label": "Ownership", "text": "You lead multi-disciplined teams to deliver organizational objectives."},
            {"label": "Autonomy", "text": "Operates with high autonomy across organizational boundaries. Trusted to make strategic and operational decisions that affect major parts of the business. Seeks input to enhance alignment and transparency, while owning the decision."},
            {"label": "Communication", "text": "You communicate effectively at all levels. You cultivate a safe space and culture where continuous and constructive feedback are welcomed and encouraged."},
            {"label": "Problem Solving", "text": "You build strategic internal and external partnerships to solve business challenges."},
        ],
        "leadership": "You serve as a distinguished role model of company tenets and embed them into strategic decision making. You develop and mentor people across the company, and coach your leaders to do the same. You hold the company accountable to an inclusive environment that’s welcoming to all. You are able to communicate an inspiring vision those inside and outside of the company. You delegate to empower team members. You develop others through effective stretch assignments, projects and initiatives. Manages performance of multiple teams or department to successful outcomes.",
    },
    10: {
        "who_you_are": "Senior VP commonly in a people manager role.",
        "results": [
            {"label": "Expertise", "text": "You lead large, global and matrixed teams to deliver on complex and business-critical initiatives."},
            {"label": "Scope", "text": "Your work supports company level goals. You are accountable for initiatives, projects and programs that directly impact business results and our reputation in the industry."},
        ],
        "behaviors": [
            {"label": "Collaboration", "text": "You develop relationships with strategic stakeholders both inside and outside of the company."},
            {"label": "Ownership", "text": "You direct the work of organizations to deliver on company objectives. Create future-focused strategies that drive competitive advantage."},
            {"label": "Autonomy", "text": "Operates with full trust and autonomy to shape enterprise vision and long-term direction. Provides oversight to others rather than receiving it. Exercises independent judgment in complex, high-stakes environments."},
            {"label": "Communication", "text": "You calmly and credibly handle strategic communication issues, even in high-stakes situations."},
            {"label": "Problem Solving", "text": "You make bold decisions that may be without precedent or move toward uncharted strategic areas."},
        ],
        "leadership": "You champion our tenets and hold others accountable for demonstrating them. You develop and mentor people across the company, and coach your managers, directors & VPs to do the same. You hold the company accountable to an inclusive environment that’s welcoming to all. You delegate to empower team members. You develop others through effective stretch assignments, projects and initiatives. Build a strong leadership pipeline and inspire people to work together toward a shared vision with common goals. Manages performance of multiple teams or department to successful outcomes.",
    },
}


def level_for_title(title: Optional[str]) -> Optional[int]:
    """Return the numeric level for a career-ladder title, or None if unknown."""
    if not title:
        return None
    return TITLE_TO_LEVEL.get(title.strip())


def get_competency_payload() -> dict:
    """Shape suitable for JSON-embedding on the Team Members page."""
    return {
        "titleToLevel": TITLE_TO_LEVEL,
        "levelLabels": LEVEL_LABELS,
        "competencies": COMPETENCIES,
    }
