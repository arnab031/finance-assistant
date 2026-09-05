# -*- coding: utf-8 -*-
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deck_lib import *

prs = new_deck()
N = [0]
def page(bg=GROUND, no_footer=False):
    s = blank(prs, bg)
    N[0] += 1
    if not no_footer:
        footer(s, N[0])
    return s

# ============================================================ 1 · TITLE
s = page(DARK, no_footer=True)
rect(s, 0, 0, 0.14, H, fill=ACCENT, rounded=False)
T(s, 1.25, 1.42, 9.0, 0.4,
  [([("TBX / BVP TECH CATALYST  ·  BUILD A FINANCE ASSISTANT THAT ACTUALLY UNDERSTANDS YOU",
      "eyebrow", {"color": ACCENT_LT, "size": 10.5})], {})])
T(s, 1.22, 1.80, 9.6, 1.1, [([("Finsight", "h1d")], {})])
T(s, 1.25, 2.92, 9.6, 0.5,
  [([("Grounded answers from your bank ledger.", "h1",
      {"color": ACCENT_LT, "size": 21, "bold": False})], {})])
hair(s, 1.25, 3.62, 3.2, C(0x2A,0x3B,0x3F))
T(s, 1.25, 3.82, 8.8, 1.0,
  [([("Ask about spending, income, net movement and who was paid, in plain language. ", "body",
      {"color": DARK_INK2, "size": 13}),
     ("Every figure is computed in SQL and checked against the source rows before it reaches "
      "the screen — no number the assistant states can originate from the model.", "body",
      {"color": DARK_INK, "size": 13, "bold": True})], {"ls": 1.35})])

cards = [("46/46", "golden canary, 100%"),
         ("7.6 B", "local model · 20 B ceiling"),
         ("2", "model calls per question"),
         ("0", "numbers authored by the model")]
cx, cw, gap = 1.25, 2.28, 0.24
for big, lbl in cards:
    rect(s, cx, 5.28, cw, 1.05, fill=DARK2, line=C(0x27,0x35,0x39), lw=0.9, radius=0.10)
    T(s, cx+0.18, 5.42, cw-0.3, 0.42, [([(big, "big", {"color": ACCENT_LT, "size": 20})], {})])
    T(s, cx+0.18, 5.82, cw-0.3, 0.44, [([(lbl, "tiny", {"color": DARK_INK2})], {"ls": 1.1})])
    cx += cw + gap
T(s, 1.25, 6.72, 9.0, 0.3, [([("Team Finsight  ·  Section 6 deliverable: presentation deck", "tiny",
                              {"color": C(0x60,0x72,0x74)})], {})])

# ============================================================ 2 · WHAT WE BUILT
s = page()
y = header(s, "what we built",
           "Ask in plain language. Get a figure you can trace.",
           "A chat assistant over the organizers' bank-transaction schema — MySQL 8.4, a 7.6 B local model, "
           "and a pipeline where the model never touches a number.")

LW = 5.65
T(s, ML, y, LW, 3.4, [
  ([("Aggregates. ", "bold"), ("Spend, income, net movement, counts — over any period the data covers.", "body")], {"space_after": 9}),
  ([("Who was paid. ", "bold"), ("This schema has no vendor table; payee names live inside free-text "
     "descriptions. A derived ", "body"), ("counterparty", "monod"), (" column is what makes the question answerable at all.", "body")], {"space_after": 9}),
  ([("Records. ", "bold"), ("Largest transactions, anything matching a filter — with the full row set and Export CSV.", "body")], {"space_after": 9}),
  ([("Follow-ups. ", "bold"), ("Threads persist; a clarification sticks for the rest of the conversation.", "body")], {"space_after": 9}),
  ([("Refusals. ", "bold"), ("When the schema cannot answer, it says which domain is missing — rather than "
     "inventing one, or returning ", "body"), ("₹0.00", "monod"), (".", "body")], {"space_after": 9}),
  ([("Anomalies. ", "bold"), ("A windowed z-score against the account's own history.", "body")], {}),
], anchor=MSO_ANCHOR.TOP)

# chat mock
MX = ML + LW + 0.55
MW = CW - LW - 0.55
rect(s, MX, y-0.06, MW, 3.62, fill=SURFACE, line=LINE, lw=1.0, radius=0.05)
rect(s, MX+0.28, y+0.20, MW-0.56, 0.46, fill=SURFACE2, line=None, radius=0.30)
T(s, MX+0.28, y+0.20, MW-0.56, 0.46,
  [([("How much did we spend in June 2026?", "q", {"size": 11.5})], {})],
  align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
T(s, MX+0.30, y+0.82, MW-0.60, 0.92,
  [([("We spent ", "a"), ("₹1,69,299.00", "a", {"bold": True, "color": ACCENT_INK}),
     (" in June 2026, based on the partial coverage data available, which spans approximately "
      "80% of the month.", "a")], {"ls": 1.3})])
hair(s, MX+0.30, y+1.86, MW-0.60, LINE2)
T(s, MX+0.30, y+1.98, MW-0.60, 0.62,
  [([("SELECT SUM(t.debit_amount) AS value, COUNT(*)", "monod", {"size": 8.5}), ], {}),
   ([("FROM v_txn t WHERE t.transaction_date >= %(p_start)s", "monod", {"size": 8.5})], {})])
cx = chip(s, MX+0.30, y+2.72, "verified 3 / 3")
cx = chip(s, cx, y+2.72, "high confidence")
cx = chip(s, cx, y+2.72, "4 rows", fill=SURFACE2, color=INK2)
T(s, MX+0.30, y+3.10, MW-0.60, 0.42,
  [([("Coverage note written by the pipeline, not the model: data ends 2026-06-24.", "tiny")], {"ls": 1.15})])

callout(s, ML, 5.72, CW, 0.90,
        [("The thesis. ", "warnb"),
         ("The model decides what to compute. MySQL computes it. The model's words are then checked "
          "against the result before anyone sees them.", "bodyi")],
        fill=ACCENT_SOFT, bar=ACCENT)

# ============================================================ 3 · THE DECISION
s = page()
y = header(s, "01 · the decision everything follows from",
           "We deliberately did not build text-to-SQL")
callout(s, ML, y, CW, 0.72,
        [("The brief rules it out in one sentence: ", "body"),
         ("“aggregate the data correctly before handing results to the language model, so the model "
          "explains a computed result rather than calculating one itself.”", "bodyi", {"italic": True})],
        fill=SURFACE2, bar=INK3)
cols = [2.55, 4.15, 5.193]
grid(s, ML, y+0.94, CW, cols,
     ["", "Text-to-SQL", "Extract → compile  (ours)"],
     [["Model produces", "Free-form SQL", [("A small typed JSON object", "accent")]],
      ["Can emit invalid SQL?", "Yes", [("No", "accent"), (" — Python writes every query", "body")]],
      ["Can invent a number?", "Yes", [("No", "accent"), (" — arithmetic happens only in MySQL", "body")]],
      ["Small-model reliability", "Poor — generation is hard", [("Good", "accent"), (" — extraction is easy", "body")]],
      ["Guardrails attach", "After generation, awkwardly", [("Before compute", "accent"), (", on a typed object", "body")]]],
     row_h=0.46, head_h=0.42)
T(s, ML, 5.90, CW, 0.80,
  [([("That reframe is also what makes a 7.6 B model viable. ", "bold"),
     ("We ask the model to do extraction, not generation — and filling a fixed schema is exactly what "
      "small models are good at. The model-efficiency criterion turns from a constraint into an advantage.", "body")],
    {"ls": 1.3})])

# ============================================================ 4 · PIPELINE
s = page()
y = header(s, "02 · architecture", "The request pipeline", rule=True)

n, gap = 7, 0.30
bw = (CW - (n-1)*gap) / n
by, bh = 2.62, 1.42
steps = [
  ("LLM call 1\nextract", "question → typed QuerySpec", "llm"),
  ("Validate", "closed vocabularies;\ncontradictions rejected", "code"),
  ("Resolve", "counterparty · coverage ·\nambiguity probes", "code"),
  ("Compile → SQL", "pure Python,\nbound parameters only", "code"),
  ("MySQL 8.4", "every SUM and GROUP BY,\nin DECIMAL(15,2)", "db"),
  ("LLM call 2\nnarrate", "sees only the returned rows;\nforbidden to compute", "llm"),
  ("Verify", "every figure must exist\nin those rows", "code"),
]
centers = []
for i, (title, sub, kind) in enumerate(steps):
    x = ML + i*(bw+gap)
    centers.append(x + bw/2)
    if kind == "llm":
        sh = rect(s, x, by, bw, bh, fill=SURFACE, line=ACCENT, lw=1.6, dash=DASH.DASH, radius=0.08)
        tc, sc = ACCENT_INK, ACCENT_INK
    elif kind == "db":
        sh = rect(s, x, by, bw, bh, fill=ACCENT_SOFT, line=ACCENT, lw=2.0, radius=0.08)
        tc, sc = ACCENT_INK, ACCENT_INK
    else:
        sh = rect(s, x, by, bw, bh, fill=SURFACE, line=LINE, lw=1.1, radius=0.08)
        tc, sc = INK, INK2
    tfr = tf_of(sh, pad=0.10, anchor=MSO_ANCHOR.MIDDLE)
    paras = []
    for j, ln in enumerate(title.split("\n")):
        paras.append(([(ln, "bold", {"size": 11, "color": tc})],
                      {"align": PP_ALIGN.CENTER, "space_after": 0}))
    paras[-1][1]["space_after"] = 5
    for ln in sub.split("\n"):
        paras.append(([(ln, "tiny", {"size": 8.5, "color": sc if kind != "code" else INK3})],
                      {"align": PP_ALIGN.CENTER, "ls": 1.05, "space_after": 0}))
    write(tfr, paras)
    if i < n-1:
        arrow(s, x+bw+0.055, by+bh/2, x+bw+gap-0.055, by+bh/2, INK3, 1.1)

# question pill
rect(s, ML, 1.88, bw*2+gap, 0.56, fill=SURFACE2, line=None, radius=0.14)
T(s, ML+0.14, 1.92, bw*2+gap-0.28, 0.50,
  [([("User question", "bold", {"size": 10})], {"align": PP_ALIGN.CENTER, "space_after": 1}),
   ([("“How much did we spend in June 2026?”", "tiny")], {"align": PP_ALIGN.CENTER})])
arrow(s, centers[0], 2.46, centers[0], by-0.04, INK3, 1.1)

# answer pill
apw = bw*2+gap
apx = W - MR - apw
rect(s, apx, 4.58, apw, 0.56, fill=SURFACE2, line=None, radius=0.14)
T(s, apx+0.14, 4.62, apw-0.28, 0.50,
  [([("Answer", "bold", {"size": 10})], {"align": PP_ALIGN.CENTER, "space_after": 1}),
   ([("prose + breakdown table + provenance panel", "tiny")], {"align": PP_ALIGN.CENTER})])
arrow(s, centers[6], by+bh+0.04, centers[6], 4.54, INK3, 1.1)

# branch notes
T(s, centers[2]-1.05, by+bh+0.04, 2.10, 0.40,
  [([("material gap → ask the user,", "tiny", {"color": ACCENT_INK})], {"align": PP_ALIGN.CENTER, "space_after": 0}),
   ([("both readings priced first", "tiny", {"color": ACCENT_INK})], {"align": PP_ALIGN.CENTER})])
T(s, centers[5]-1.05, by+bh+0.04, 2.10, 0.40,
  [([("fails → retry, then", "tiny", {"color": WARN})], {"align": PP_ALIGN.CENTER, "space_after": 0}),
   ([("a template built from the rows", "tiny", {"color": WARN})], {"align": PP_ALIGN.CENTER})])

# legend
lx = ML
rect(s, lx, 5.42, 0.34, 0.20, fill=SURFACE, line=ACCENT, lw=1.5, dash=DASH.DASH, radius=0.3)
T(s, lx+0.46, 5.37, 3.9, 0.3, [([("model — never sees a total it did not receive", "tiny")], {})])
rect(s, lx+4.40, 5.42, 0.34, 0.20, fill=SURFACE, line=LINE, lw=1.1, radius=0.3)
T(s, lx+4.86, 5.37, 4.2, 0.3, [([("code — deterministic, testable, no model", "tiny")], {})])

callout(s, ML, 5.85, CW, 0.80,
        [("Exactly two model calls per question — and neither one can put a number on screen. ", "warnb", {"color": ACCENT_INK}),
         ("Rows stream to the browser at the MySQL step, before narration begins, so real figures appear "
          "while the prose is still being written.", "bodyi")],
        fill=ACCENT_SOFT, bar=ACCENT)

# ============================================================ 5 · GROUNDING
s = page()
y = header(s, "03 · grounding", "Five places a wrong answer gets stopped",
           "Accuracy and grounding is the largest single criterion. Each layer is cheap, each catches a "
           "different failure, and every example below is from this database.")
cols = [2.30, 3.35, 6.243]
grid(s, ML, y, CW, cols,
     ["Layer", "Catches", "Example on this data"],
     [
      [[("1 · Schema", "bold")], "Invented dimensions, metrics or statuses; contradictory date fields",
       "A metric outside the declared set is rejected and repaired on retry"],
      [[("2 · Sanity pass", "bold")], "Right shape, wrong question — and filters nobody asked for",
       [("“spend” routed to ", "body"), ("gross_amount", "monod"), (": ", "body"),
        ("₹5,46,616 instead of ₹2,49,806", "warnb"), (", because gross adds credits to debits", "body")]],
      [[("3 · Coverage", "bold")], "Periods outside the data",
       [("“last month” → ", "body"), ("No data for August 2026. Coverage runs 2025-12-03 to 2026-06-24", "bodyi", {"italic": True}),
        (" — never ", "body"), ("₹0.00", "monod")]],
      [[("4 · Ambiguity", "bold")], "Questions with two defensible answers",
       [("“the total for this account” — net movement or throughput? Both readings are ", "body"),
        ("priced", "accent"), (" before either is shown", "body")]],
      [[("5 · Provenance", "bold")], "Any figure in the prose absent from the rows",
       [("Verification failure drops confidence to ", "body"), ("low", "warnb"),
        (" and replaces the narration with a template built from the rows", "body")]],
     ],
     row_h=[0.60, 0.60, 0.60, 0.60, 0.60], head_h=0.42)

callout(s, ML, 5.70, CW*0.485, 0.88,
        [("Sensitive fields never reach the model. ", "warnb"),
         ("Views serve masked forms only; rows carry AES-256-SIV ciphertext, substituted back after verification.", "bodyi", {"size": 10})])
callout(s, ML+CW*0.515, 5.70, CW*0.485, 0.88,
        [("Structural, not promised. ", "warnb", {"color": ACCENT_INK}),
         ("The compiler emits only columns the profile declares; the verifier rejects any figure absent from the rows.", "bodyi", {"size": 10})],
        fill=ACCENT_SOFT, bar=ACCENT)

# ============================================================ 6 · AMBIGUITY
s = page()
y = header(s, "04 · layer four, slowly", "Ambiguity is measured, not guessed")
LW2 = 6.15
T(s, ML, y, LW2, 3.2, [
  ([("Ambiguity is not a property of the question — it is a property of the question ", "body"),
    ("against this data", "bold"), (". “How much did we spend last month” is ambiguous in principle; "
    "if the competing readings land on the same number it is not ambiguous in fact, and asking is friction.", "body")],
   {"ls": 1.32, "space_after": 12}),
  ([("So we never ask the model whether something is ambiguous. ", "bold"),
    ("Rules detect the candidate readings, each one is run as a cheap scalar query, and the decision comes "
     "from the measured gap between them.", "body")], {"ls": 1.32, "space_after": 12}),
  ([("When we do ask, we show the real number for each option — so the user recognises their own intent "
     "instead of parsing jargon.", "body")], {"ls": 1.32}),
])
rect(s, ML, y+2.72, 2.55, 0.52, fill=ACCENT_SOFT, line=None, radius=0.14)
T(s, ML, y+2.72, 2.55, 0.52, [([("Zero extra model calls", "accent", {"size": 11.5})], {})],
  align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)

BX = ML + LW2 + 0.55
BW = CW - LW2 - 0.55
bands = [("under 1%", "Answer silently.", "The fork does not change the answer."),
         ("1 – 10%", "Answer, and state the assumption.", "The user sees which reading was taken."),
         ("over 10%", "Ask — with both numbers shown.", "Two defensible answers, materially apart.")]
byy = y
for i, (rng, act, why) in enumerate(bands):
    hgt = 0.94
    accent_on = (i == 2)
    rect(s, BX, byy, BW, hgt, fill=SURFACE, line=ACCENT if accent_on else LINE,
         lw=1.4 if accent_on else 0.9, radius=0.08)
    rect(s, BX, byy, 0.055, hgt, fill=ACCENT if accent_on else LINE, rounded=False)
    T(s, BX+0.26, byy+0.13, BW-0.5, 0.70, [
        ([(rng, "bold", {"size": 12, "color": ACCENT_INK if accent_on else INK}),
          ("   measured gap", "tiny")], {"space_after": 3}),
        ([(act, "body", {"size": 10.5})], {"space_after": 2}),
        ([(why, "tiny")], {}),
    ])
    byy += hgt + 0.19

callout(s, ML, 5.86, CW, 0.90,
        [("Percentage alone was the wrong test. ", "warnb"),
         ("A fork can sit below the ask threshold in percentage terms and still be worth crores in absolute "
          "terms. A second trigger now fires on absolute gap — 0.5% of total spend, so it scales with "
          "whatever dataset is loaded.", "bodyi")])

# ============================================================ 7 · THE TRAP
s = page()
y = header(s, "05 · the bug worth showing", "A confident, verified, false answer")
T(s, ML, y, CW*0.52, 2.6, [
  ([("SUM()", "monod", {"size": 11}), (" over nothing returns ", "body"), ("one row", "bold"),
    (" — ", "body"), ("(NULL, 0)", "monod", {"size": 11}), (" — not zero rows.", "body")],
   {"ls": 1.32, "space_after": 10}),
  ([("So every ", "body"), ("if not rows", "monod", {"size": 11}),
    (" check is blind to it, and the model reads that row as the number zero.", "body")],
   {"ls": 1.32, "space_after": 10}),
  ([("Zero means “these netted to nothing.” The truth was “no such transactions.” "
     "The pipeline now collapses that row and reports that the filters matched nothing — which is "
     "not the same as a total of zero.", "body")], {"ls": 1.32}),
])
QX = ML + CW*0.545
QW = CW - CW*0.545
rect(s, QX, y-0.04, QW, 2.30, fill=WARN_SOFT, line=None, radius=0.06)
rect(s, QX, y-0.04, 0.05, 2.30, fill=WARN, rounded=False)
T(s, QX+0.28, y+0.16, QW-0.56, 1.0,
  [([("“The sum of all transactions regarding the account number 30123456789012 is Rs 0.”",
      "bodyi", {"size": 12.5, "italic": True})], {"ls": 1.30})])
cx = chip(s, QX+0.28, y+1.28, "verified", fill=SURFACE, color=INK2)
cx = chip(s, cx, y+1.28, "high confidence", fill=SURFACE, color=INK2)
cx = chip(s, cx, y+1.28, "and false", fill=WARN, color=SURFACE)
T(s, QX+0.28, y+1.70, QW-0.56, 0.45,
  [([("Measured on this data, before the case was handled.", "tiny", {"color": WARN})], {})])

hair(s, ML, 5.05, CW)
T(s, ML, 5.28, CW, 1.2, [
  ([("Bugs of this shape do not throw — they render. ", "bold"),
    ("Nothing in the stack was failing: the query ran, a row came back, the verifier found the figure in "
     "that row, and the badge went green. The only thing that catches it is asking “is the number "
     "right?” against independently computed ground truth — which is what the 46-question canary "
     "exists to do, and why its expected values are recomputed queries rather than stored literals.", "body")],
   {"ls": 1.34}),
])

# ============================================================ 8 · DATA MODEL
s = page()
y = header(s, "06 · data model", "Three tables, two foreign keys, one view doing the work")

tb_y, tb_h = 1.98, 1.44
tbl = [(ML, 3.05, "bank", ["bank_code · bank_name"], "10 rows", False),
       (ML+3.45, 3.40, "account", ["account_id · entity_id", "program_id · balance"], "10 rows", False),
       (ML+7.30, 4.593, "transaction", ["date · type · amount", "description · reference"], "10 rows · 9 counterparties", True)]
for x, w, name, lines, foot, hot in tbl:
    rect(s, x, tb_y, w, tb_h, fill=ACCENT_SOFT if hot else SURFACE,
         line=ACCENT if hot else LINE, lw=1.7 if hot else 1.0, radius=0.08)
    paras = [([(name, "bold", {"size": 12.5, "color": ACCENT_INK if hot else INK})],
              {"align": PP_ALIGN.CENTER, "space_after": 4})]
    for ln in lines:
        paras.append(([(ln, "tiny", {"font": MONO, "size": 8.5, "color": ACCENT_INK if hot else INK2})],
                      {"align": PP_ALIGN.CENTER, "space_after": 1}))
    if name == "account":
        paras.append(([("account_number", "tiny", {"font": MONO, "size": 8.5, "color": WARN, "bold": True}),
                       ("  SENSITIVE", "tiny", {"size": 7, "color": WARN, "bold": True})],
                      {"align": PP_ALIGN.CENTER, "space_after": 1}))
    if hot:
        paras.append(([("utr_number", "tiny", {"font": MONO, "size": 8.5, "color": WARN, "bold": True}),
                       ("  SENSITIVE", "tiny", {"size": 7, "color": WARN, "bold": True})],
                      {"align": PP_ALIGN.CENTER, "space_after": 1}))
        paras.append(([("counterparty", "tiny", {"font": MONO, "size": 8.5, "color": ACCENT_INK, "bold": True}),
                       ("  DERIVED", "tiny", {"size": 7, "color": ACCENT_INK, "bold": True})],
                      {"align": PP_ALIGN.CENTER, "space_after": 3}))
    paras.append(([(foot, "tiny", {"size": 7.5})], {"align": PP_ALIGN.CENTER}))
    write(tf_of(rect(s, x, tb_y, w, tb_h, fill=None, line=None), pad=0.10, anchor=MSO_ANCHOR.MIDDLE), paras)

arrow(s, ML+3.45, tb_y+0.52, ML+3.11, tb_y+0.52, INK3, 1.1)
T(s, ML+3.05, tb_y+0.13, 0.45, 0.3, [([("bank_code", "tiny", {"size": 7})], {"align": PP_ALIGN.CENTER})])
arrow(s, ML+7.30, tb_y+0.52, ML+6.91, tb_y+0.52, INK3, 1.1)
T(s, ML+6.86, tb_y+0.13, 0.50, 0.3, [([("account_id", "tiny", {"size": 7})], {"align": PP_ALIGN.CENTER})])

for cx0 in [ML+3.05/2, ML+3.45+3.40/2, ML+7.30+4.593/2]:
    arrow(s, cx0, tb_y+tb_h+0.03, cx0, 3.86, INK3, 1.0, dash=DASH.DASH)

vy = 3.90
rect(s, ML, vy, CW, 1.42, fill=SURFACE, line=ACCENT, lw=1.8, radius=0.06)
T(s, ML+0.3, vy+0.14, CW-0.6, 1.2, [
  ([("v_txn", "bold", {"size": 13, "font": MONO, "color": ACCENT_INK}),
    ("  —  the only surface the SQL compiler may touch", "bold", {"size": 13, "color": ACCENT_INK})],
   {"align": PP_ALIGN.CENTER, "space_after": 6}),
  ([("joins all three · denormalises bank and account onto every row · splits the unsigned amount into "
     "credit_amount / debit_amount / signed_amount", "body", {"size": 10})], {"align": PP_ALIGN.CENTER, "space_after": 4}),
  ([("serves masked forms only — XXXXXX3445, UTR-1CRl — so a raw value cannot reach an answer even "
     "if a later query forgets to mask", "warn", {"size": 10})], {"align": PP_ALIGN.CENTER, "space_after": 4}),
  ([("companions:  v_account · v_data_coverage · v_health · v_incidents · v_model_scorecard", "tiny", {"size": 8.5})],
   {"align": PP_ALIGN.CENTER}),
])

T(s, ML, 5.42, CW, 1.46, [
  ([("Amounts are unsigned. ", "bold", {"size": 10}), ("Direction lives in ", "body", {"size": 10}), ("transaction_type", "monod", {"size": 9}),
    (", so the view pre-splits it and the compiler keeps using plain ", "body", {"size": 10}), ("SUM()", "monod", {"size": 9}),
    (" rather than learning conditional aggregation. “Spend” is debits only.", "body", {"size": 10})],
   {"ls": 1.24, "space_after": 6, "size": 10}),
  ([("There is no counterparty table. ", "bold", {"size": 10}), ("Payee names are buried in free-text ", "body", {"size": 10}),
    ("description", "monod", {"size": 9}), ("; ", "body", {"size": 10}), ("counterparty", "monod", {"size": 9}),
    (" is extracted at boot and backfilled across the whole table — the only reason "
     "“who did we pay?” has an answer.", "body", {"size": 10})], {"ls": 1.24, "space_after": 6, "size": 10}),
  ([("Nothing hardcodes the sample. ", "bold", {"size": 10}),
    ("Coverage, vocabularies and the money columns are introspected into one registry at boot, so swapping "
     "in the full export is a load, not a code change.", "body", {"size": 10})], {"ls": 1.24}),
])

# ============================================================ 9 · MODEL CHOICE
s = page()
y = header(s, "07 · model choice", "A 7.6 B model — because the architecture lets it be",
           "The constraint is the lowest possible model at the highest possible accuracy. "
           "The model never generates SQL and never does arithmetic; both of its jobs are extraction.")
LW3 = 6.55
items = [("Schema-constrained decoding",
          "The QuerySpec JSON Schema is passed to the serving layer's structured-output constraint, so an "
          "invalid shape is impossible rather than merely unlikely."),
         ("Few-shot examples from observed failures",
          "Each example targets a failure seen in the zero-shot baseline probe — which scored about "
          "2/5 semantically correct — not an imagined one."),
         ("Pre-resolved date windows",
          "api/dates.py hands the model a closed set of periods, so it selects a window instead of deriving one.")]
yy = y
for i, (t, d) in enumerate(items):
    T(s, ML, yy, 0.42, 0.4, [([(str(i+1), "bigA", {"size": 19})], {})])
    T(s, ML+0.52, yy+0.04, LW3-0.52, 1.0, [
        ([(t, "bold", {"size": 12})], {"space_after": 3}),
        ([(d, "body", {"size": 10.5})], {"ls": 1.26}),
    ])
    yy += 1.12

MX2 = ML + LW3 + 0.50
MW2 = CW - LW3 - 0.50
rect(s, MX2, y-0.04, MW2, 3.34, fill=SURFACE, line=LINE, lw=1.0, radius=0.05)
T(s, MX2+0.28, y+0.16, MW2-0.56, 3.0, [
  ([("SERVING STACK", "eyebrow", {"size": 9})], {"space_after": 9}),
  ([("qwen2.5-7b-instruct", "bold", {"size": 13, "font": MONO, "color": ACCENT_INK})], {"space_after": 2}),
  ([("7.6 B parameters · 4-bit · 4.7 GB · local", "tiny")], {"space_after": 11}),
  ([("Ceiling in the brief is 20 B. ", "body", {"size": 10.5}),
    ("This runs on a laptop with no API credits spent.", "body", {"size": 10.5})], {"ls": 1.25, "space_after": 11}),
  ([("nomic-embed-text", "monod", {"size": 10, "color": ACCENT_INK}), ("  ·  137 M", "tiny")], {"space_after": 2}),
  ([("semantic entity resolution, off by default", "tiny")], {"space_after": 11}),
  ([("claude-haiku-4-5", "monod", {"size": 10, "color": ACCENT_INK}), ("  ·  fallback", "tiny")], {"space_after": 2}),
  ([("same interface — a two-line .env change", "tiny")], {}),
])

callout(s, ML, 5.86, CW, 0.90,
        [("Comparing models is a measurement, not an opinion. ", "warnb", {"color": ACCENT_INK}),
         ("The /ops page runs the same 46-question canary against any configured model and keeps a scorecard "
          "over time, so “is the smaller model good enough?” has an answer with a date on it.", "bodyi")],
        fill=ACCENT_SOFT, bar=ACCENT)

# ============================================================ 10 · vLLM
s = page()
y = header(s, "08 · serving the model", "Same model, same accuracy, 2.7× lower latency",
           "Qwen2.5-7B-Instruct-4bit served locally through vLLM on Apple Silicon (MLX), behind an "
           "OpenAI-compatible /v1/chat/completions API with structured-output constraints.")
grid(s, ML, y, CW, [4.30, 3.80, 3.793],
     ["Metric", "vLLM — Qwen2.5-7B-4bit", "Ollama — Qwen2.5-7B-4bit"],
     [["Accuracy", [("38 / 40  (95%)", "accent")], [("38 / 40  (95%)", "bold")]],
      ["Median query latency (p50)", [("2.26 s", "accent")], [("6.07 s", "bold")]],
      ["Total wall time, 40 queries", [("113.92 s", "accent")], [("262.2 s", "bold")]],
      ["Numeric questions", [("19 / 19", "accent")], [("19 / 19", "bold")]],
      ["Behaviour questions", [("11 / 11", "accent")], [("11 / 11", "bold")]]],
     row_h=0.44, head_h=0.42)
T(s, ML, y+2.72, CW, 0.30,
  [([("Identical 4-bit weights and identical prompts on both sides — the win is in the serving layer, "
      "not the model.", "muted")], {})])
callout(s, ML, y+3.10, CW, 0.66,
        [("Read the two latency figures separately. ", "warnb"),
         ("2.26 s is the median of this 40-query serving benchmark. The 4.1 s p50 on the results slide is "
          "end-to-end across 1,315 logged production requests on the Ollama path — a different "
          "measurement, not a contradiction.", "bodyi", {"size": 10.5})])
cw2 = (CW - 0.45) / 2
T(s, ML, 6.00, cw2, 0.85, [
  ([("Structured output", "bold", {"size": 11.5})], {"space_after": 4}),
  ([("Uses ", "body", {"size": 10}), ("guidance", "monod", {"size": 9}),
    (" as the structured-decoding backend, with unrestricted whitespace generation disabled — which "
     "makes JSON extraction reliable.", "body", {"size": 10})], {"ls": 1.22}),
])
T(s, ML+cw2+0.45, 6.00, cw2, 0.85, [
  ([("Why not Gemma", "bold", {"size": 11.5})], {"space_after": 4}),
  ([("A Gemma-based model degraded on the long production prompt — whitespace and repetition loops. "
     "Qwen2.5 produced valid, semantically correct output.", "body", {"size": 10})], {"ls": 1.22}),
])

# ============================================================ 11 · SAMPLES
s = page()
y = header(s, "09 · sample questions", "What it actually answered",
           "Captured from a live run against tbx_live on 2026-09-05. Coverage is 2025-12-03 → 2026-06-24, "
           "10 transactions, INR — including the refusals.")
cw3 = (CW - 0.42) / 2
rows = [
 (ML, y, "How much did we spend in total?",
  [("We spent a total of ", "a"), ("₹2,49,806.00", "a", {"bold": True}), (".", "a")],
  ["verified 1/1", "high", "2.8 s"],
  "The metric is debit_amount, not the gross sum — summing both directions would report throughput.", False),
 (ML+cw3+0.42, y, "Who did we pay the most?",
  [("The counterparty we paid the most was ", "a"), ("SELECTION MOBILE", "a", {"bold": True}),
   (", with a payment value of ", "a"), ("₹146,474.00", "a", {"bold": True}), (".", "a")],
  ["verified 1/1", "high", "3.2 s"],
  "Answerable only because counterparty is derived from free text at boot — this schema has no vendor table.", False),
 (ML, y+2.08, "How much did we spend last month?",
  [("No data for August 2026. Coverage runs 2025-12-03 to 2026-06-24.", "a", {"bold": True})],
  ["guardrail · coverage"],
  "Not ₹0.00. Zero and unknown are different answers, and conflating them is what the grounding criterion punishes.", True),
 (ML+cw3+0.42, y+2.08, "Which transactions are still unreconciled?",
  [("This data has no reconciliation status of any kind.", "a", {"bold": True})],
  ["guardrail · absent concept"],
  "One of the brief's own example questions. The refusal names the specific missing domain, not a generic list.", True),
]
for x, yy0, q, ans, chips, note, guard in rows:
    hh = 1.96
    rect(s, x, yy0, cw3, hh, fill=SURFACE, line=WARN if guard else LINE, lw=1.2 if guard else 0.9, radius=0.05)
    rect(s, x, yy0, 0.05, hh, fill=WARN if guard else ACCENT, rounded=False)
    T(s, x+0.26, yy0+0.15, cw3-0.52, 0.34, [([(q, "q", {"size": 11.5, "color": WARN if guard else ACCENT_INK})], {})])
    T(s, x+0.26, yy0+0.52, cw3-0.52, 0.66, [(ans, {"ls": 1.26})])
    cx = x+0.26
    for c in chips:
        cx = chip(s, cx, yy0+1.24, c, fill=WARN_SOFT if guard else ACCENT_SOFT, color=WARN if guard else ACCENT_INK)
    T(s, x+0.26, yy0+1.56, cw3-0.52, 0.34, [([(note, "tiny")], {"ls": 1.1})])

callout(s, ML, 6.24, CW, 0.62,
        [("Filtering is not displaying. ", "warnb"),
         ("“How much went through account 50200013729069” is answered normally — the number came from the "
          "user. Listing or displaying account and UTR numbers is refused.", "bodyi", {"size": 10.5})])

# ============================================================ 12 · RESULTS
s = page()
y = header(s, "10 · results", "What we measured",
           "Latest run — qwen2.5:7b-instruct, 2026-09-05.")
scw = (CW - 3*0.26) / 4
for i, (big, lbl, acc) in enumerate([
        ("46 / 46", "golden canary, 100% in 202 s — 21 graded on the number, 14 on behaviour, 11 on the spec", True),
        ("69 / 69", "unit tests — 30 compiler, 18 narration, 12 extraction, 9 semantic", False),
        ("97.4%", "verification pass rate, against a ≥ 95% threshold", False),
        ("93.6%", "answers at high confidence, against a ≥ 85% threshold", False)]):
    statcard(s, ML + i*(scw+0.26), y, scw, 1.32, big, lbl, accent=acc)

yy = y + 1.58
cwl = (CW - 0.45) * 0.46
rect(s, ML, yy, cwl, 1.30, fill=SURFACE, line=LINE, lw=0.9, radius=0.06)
T(s, ML+0.26, yy+0.16, cwl-0.52, 1.05, [
  ([("Latency", "bold", {"size": 12})], {"space_after": 6}),
  ([("p50 ", "muted"), ("4.1 s", "bold", {"size": 12}), ("      p95 ", "muted"), ("14.2 s", "bold", {"size": 12}),
    ("      over 1,315 logged requests", "tiny")], {"space_after": 5}),
  ([("The same model medians ", "body", {"size": 10}),
    ("2.26 s", "accent", {"size": 10}), (" on the vLLM serving path.", "body", {"size": 10})], {"ls": 1.22}),
])
rect(s, ML+cwl+0.45, yy, CW-cwl-0.45, 1.30, fill=SURFACE, line=LINE, lw=0.9, radius=0.06)
T(s, ML+cwl+0.71, yy+0.16, CW-cwl-0.97, 1.05, [
  ([("Ground truth is a query, not a literal", "bold", {"size": 12})], {"space_after": 6}),
  ([("Every expected value in the canary is recomputed on each run, so the 46-question set stays valid the "
     "moment the organizers' full export replaces the sample rows — no answer key to maintain.", "body", {"size": 10})],
   {"ls": 1.24}),
])

yy2 = yy + 1.56
hair(s, ML, yy2, CW)
T(s, ML, yy2+0.20, CW, 0.30,
  [([("/ops", "bold", {"size": 12, "font": MONO, "color": ACCENT_INK}),
     ("   — “it answered” and “it answered correctly” are different questions", "bold", {"size": 12})], {})])
c3 = (CW - 2*0.36) / 3
for i, (t, d) in enumerate([
    ("Canary runner", "The 46 golden questions against any configured model, with a scorecard comparing models over time."),
    ("Health signals", "12 thresholded rates over 24 h — verification, template fallback, repair, clarify, empty result, p95. /api/metrics returns ok:false when any breaches."),
    ("Incidents + replay", "Every request logged with its spec, SQL, row sample, timings and confidence; tripped requests are replayable.")]):
    xx = ML + i*(c3+0.36)
    T(s, xx, yy2+0.62, c3, 0.90, [
        ([(t, "bold", {"size": 10.5, "color": ACCENT_INK})], {"space_after": 3}),
        ([(d, "tiny")], {"ls": 1.18}),
    ])

# ============================================================ 13 · CONSTRAINTS
s = page()
y = header(s, "11 · constraints", "Every constraint in the brief, and how it is met")
grid(s, ML, y, CW, [3.35, 8.543],
     ["Constraint", "How it is met"],
     [[[("≤ 20 B parameter LLM", "bold")],
       [("qwen2.5-7b-instruct", "monod"), (" — ", "body"), ("7.6 B", "accent"),
        (", 4-bit, 4.7 GB. Runs on a laptop, no API credits spent. Embedding model 137 M.", "body")]],
      [[("≤ 20 M records", "bold")],
       [("Indexed for it: ", "body"), ("idx_txn_date", "monod"), (", ", "body"), ("idx_txn_account", "monod"),
        (", ", "body"), ("idx_txn_counterparty", "monod"),
        (", FULLTEXT on description and counterparty. The two things to revisit at that scale are named on the next slide.", "body")]],
      [[("Grounded in the provided schema only", "bold")],
       [("Structural, not promised — the compiler emits only columns the profile declares, and the "
         "verifier rejects any figure absent from the result rows.", "body")]],
      [[("Single company, single currency", "bold")],
       [("INR throughout: ₹ with Indian digit grouping (₹1,69,299.00, not ₹169,299.00), "
         "timestamps stored UTC and displayed IST.", "body")]],
      [[("No fabricated figures", "bold")],
       [("The verifier enforces it per answer; the rate is a monitored signal on ", "body"), ("/ops", "monod"),
        (" — currently ", "body"), ("97.4%", "accent"), (" against a ≥ 95% threshold.", "body")]],
      [[("Sensitive fields", "bold"), ("\naccount_number, utr_number", "tiny", {"font": MONO})],
       [("Views serve masked forms only. Rows handed to the model carry AES-256-SIV ciphertext in place of "
         "the account number, substituted back at final render, ", "body"), ("after", "bold"),
        (" verification — and because SIV is deterministic, the ciphertext doubles as a stable pseudonym.", "body")]],
     ],
     row_h=[0.56, 0.74, 0.56, 0.56, 0.56, 0.78], head_h=0.42)

callout(s, ML, 6.00, CW, 0.78,
        [("Also delivered. ", "warnb", {"color": ACCENT_INK}),
         ("CSV export on any breakdown · confidence signalling (high / medium / low) on every answer · "
          "anomaly callouts via a windowed z-score against the account's own history · a provenance panel "
          "showing the spec, the SQL, the rows and every note the pipeline attached.", "bodyi")],
        fill=ACCENT_SOFT, bar=ACCENT)

# ============================================================ 14 · LIMITS
s = page()
y = header(s, "12 · known limits", "Stated plainly, because a judge will find them anyway")
lims = [
 ("Multi-turn anchors on today, not on the previous turn",
  "“How much did we spend in June 2026?” → “How does that compare to May?” works. “And the month before?” "
  "resolves to the month before today, not before June, and lands outside coverage. Named periods in a "
  "follow-up are reliable; bare relative anchors are not."),
 ("Compare answers can mislabel which period is which",
  "The SQL labels the two sides 'current' / 'previous' rather than the real period names, so the narrator "
  "infers which month is which and sometimes gets it backwards. Every number is real, so the verifier "
  "passes — and the canary grades numbers, not labels."),
 ("Fuzzy counterparty matching degraded in the MySQL port",
  "pg_trgm gave indexed similarity scoring; MySQL has neither, and FULLTEXT matches whole words, so it "
  "will not find RELIANCE inside RELIANCEDIGITAL RETAIL LTD. The profile ranks LIKE candidates by a "
  "positional score instead — correct, but it scans."),
 ("Vector search runs in Python",
  "MySQL 8.4 has no vector type, so the semantic index is a JSON array and cosine similarity runs over the "
  "loaded candidate set. Fine at this vocabulary size; MAX_LABELS refuses rather than quietly getting "
  "slow. Off by default."),
]
yy = y
for i, (t, d) in enumerate(lims):
    rect(s, ML, yy, CW, 1.04, fill=SURFACE, line=LINE, lw=0.9, radius=0.06)
    rect(s, ML, yy, 0.05, 1.04, fill=WARN, rounded=False)
    T(s, ML+0.28, yy+0.13, CW-0.56, 0.84, [
        ([(t, "bold", {"size": 11.5})], {"space_after": 3}),
        ([(d, "body", {"size": 10})], {"ls": 1.22}),
    ])
    yy += 1.14

T(s, ML, yy+0.14, CW, 0.4,
  [([("None of these is a correctness hole in the numbers — ", "bold"),
     ("each is a named boundary with a known next step.", "body")], {"ls": 1.28})])

# ============================================================ 15 · CLOSE
s = page(DARK, no_footer=True)
rect(s, 0, 0, 0.14, H, fill=ACCENT, rounded=False)
T(s, 1.25, 1.40, 9.0, 0.35,
  [([("IN ONE LINE", "eyebrow", {"color": ACCENT_LT, "size": 10.5})], {})])
T(s, 1.22, 1.84, 11.0, 2.1,
  [([("The model decides ", "h1d", {"size": 27, "bold": False}),
     ("what", "h1d", {"size": 27, "bold": True}),
     (" to compute. MySQL computes it. The model's words are then checked against the result before "
      "anyone sees them.", "h1d", {"size": 27, "bold": False})], {"ls": 1.22})])
hair(s, 1.25, 4.10, 3.2, C(0x2A,0x3B,0x3F))
T(s, 1.25, 4.32, 10.4, 0.6,
  [([("No number the assistant states can originate from the model — and that is enforced "
      "structurally, not promised.", "body", {"color": ACCENT_LT, "size": 14})], {"ls": 1.3})])

opens = [("./run.sh", "chat on :3000, ops on :3000/ops"),
         ("architecture.html", "the diagram, in a browser"),
         ("python -m eval", "the 46-question canary — expect 46/46"),
         ("README.md", "setup, samples, model choice")]
cx, cw4 = 1.25, 2.42
for cmd, d in opens:
    rect(s, cx, 5.30, cw4, 0.96, fill=DARK2, line=C(0x27,0x35,0x39), lw=0.9, radius=0.08)
    T(s, cx+0.20, 5.44, cw4-0.36, 0.75, [
        ([(cmd, "monod", {"size": 10, "color": ACCENT_LT})], {"space_after": 4}),
        ([(d, "tiny", {"color": DARK_INK2, "size": 8})], {"ls": 1.1}),
    ])
    cx += cw4 + 0.20
T(s, 1.25, 6.70, 9.0, 0.3,
  [([("Team Finsight  ·  TBX / BVP Tech Catalyst", "tiny", {"color": C(0x60,0x72,0x74)})], {})])

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "Finsight_Deck.pptx")
out = os.path.normpath(out)
prs.save(out)
print("saved", out, len(prs.slides.__iter__.__self__._sldIdLst), "slides")
