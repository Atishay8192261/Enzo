"""The workflows Enzo comes with (ADR 0032). Each is a plan in code: change one, bump its version.

Every step reads only; nothing here sends, buys or changes anything. A step that would still
waits for the user's approval in its own chat, like any helper.
"""

from enzo.workflows.definition import Input, Step, Workflow

COMPARE_PRODUCTS = Workflow(
    name="compare-products",
    title="Compare products",
    about="Reads each product page you give it, then compares them side by side.",
    version=1,
    inputs=(
        Input("pages", "the product pages to compare (their links)", many=True, max_items=8),
        Input("wants", "what matters to you, like a budget or a feature", default="nothing given"),
    ),
    steps=(
        Step(
            name="read",
            title="Read {item}",
            each="pages",
            tools=("read_page",),
            max_steps=4,
            parallel=3,
            brief=(
                "Read the product page at {item} and note what it says, from that page only:\n"
                "Name: the product's name\n"
                "Price: its price, with the currency\n"
                "In stock: yes, no, or not shown\n"
                "Details: what the page says about this: {input.wants}\n"
                "Answer with exactly those four lines, then the page's address on a fifth line. "
                "Write “not shown” for anything the page does not say: never guess. If the page "
                "cannot be read or shows no product, say so in one line instead."
            ),
        ),
        Step(
            name="compare",
            title="Compare them",
            max_steps=2,
            brief=(
                "Compare these products for the user. Use only the notes below: each was read "
                "from its own page.\n\n{steps.read}\n\nWhat matters to the user: {input.wants}\n\n"
                "Answer with a short table (name, price, in stock, details), then in two lines "
                "which one fits best and why. Name any page that could not be read. Add no facts "
                "that are not in the notes."
            ),
        ),
    ),
)

INBOX_DIGEST = Workflow(
    name="inbox-digest",
    title="Inbox digest",
    about="Reads your recent Gmail and sums up what needs you, then what is good to know.",
    version=1,
    needs=("Gmail", "/login google read"),
    inputs=(Input("period", "how far back to look", default="the last 7 days"),),
    steps=(
        Step(
            name="find",
            title="Read your inbox",
            tools=("gmail_search_threads", "gmail_get_thread"),
            max_steps=12,
            brief=(
                "Find the emails in the user's Gmail inbox from {input.period} (for the last 7 "
                "days, search “in:inbox newer_than:7d”). Open the threads that may need the user: "
                "a question to them, a bill or payment, a date or deadline, a decision, a "
                "delivery. Read at most 15. Leave out newsletters, ads and notifications that "
                "need nothing.\nFor each one, write one line: who it is from, the subject, what "
                "it needs from the user, and by when. Then one line with how many emails you "
                "looked at. Read only: send, reply to or change nothing."
            ),
        ),
        Step(
            name="digest",
            title="Write the digest",
            max_steps=2,
            brief=(
                "Write the user's inbox digest for {input.period} from these notes, read from "
                "their inbox:\n\n{steps.find}\n\nFirst “Needs you”: what needs a reply or an "
                "action, soonest first, each in one line with its date. Then “Good to know”: the "
                "rest worth knowing, each in one line. At most 12 lines in all. Add nothing that "
                "is not in the notes."
            ),
        ),
    ),
)

CHECK_MY_PRS = Workflow(
    name="check-my-prs",
    title="Check my pull requests",
    about="Looks at your open pull requests in each repository and says what needs you first.",
    version=1,
    needs=("GitHub", "/login github"),
    inputs=(
        Input("repos", "the repositories to check, as owner/name", many=True, max_items=10),
    ),
    steps=(
        Step(
            name="check",
            title="Check {item}",
            each="repos",
            tools=("github_get_me", "github_list_pull_requests", "github_pull_request_read"),
            max_steps=10,
            parallel=3,
            brief=(
                "In the GitHub repository {item}, find the open pull requests the user opened "
                "(find out who the user is first). For each one, read its checks and its reviews, "
                "and write one line: its number and title, whether its checks pass, its review "
                "state (approved, changes asked, waiting), and whether it can be merged. If the "
                "user has no open pull requests there, say so in one line. Read only: comment "
                "on, merge or change nothing."
            ),
        ),
        Step(
            name="summary",
            title="Say what needs you",
            max_steps=2,
            brief=(
                "From these notes on the user's open pull requests, one set per repository:\n\n"
                "{steps.check}\n\nTell the user what needs them first: failing checks, then "
                "changes asked, then those ready to merge, then the rest. One line per pull "
                "request, with its repository and number. Name any repository that could not be "
                "checked. Add nothing that is not in the notes."
            ),
        ),
    ),
)

BUILT_IN: tuple[Workflow, ...] = (COMPARE_PRODUCTS, INBOX_DIGEST, CHECK_MY_PRS)
