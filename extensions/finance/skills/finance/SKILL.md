---
name: finance
description: Use for the user's money: bank accounts and balances, where the month's budget stands ("où en est mon budget ce mois-ci ?"), spending by category ("combien je dépense en restaurants ?"), subscriptions and recurring charges ("mes abonnements"), net worth and holdings ("mon patrimoine"), shared household expenses ("qu'est-ce que Marie me doit ?"), and linking a bank. Paying bills or sending money is not here.
---
# Finances

Bank data comes from Enable Banking (the user's own application on their own accounts) and is synced at most every 6 hours per account: the figures are those of the last sync (`finance.sync` says when it fetched). Amounts are signed: negative = expense. Categories are French words (`courses`, `restaurants`, `transport`, `logement`, `santé`, `abonnements`, `salaire`, `impôts`, `épargne`, `autre`) from rules; the user's rules win over the built-in ones.

Tools (`read` unless said):
- `finance.accounts {}` → `{accounts: [{uid, name, bank, currency, iban, status, balance, balance_at}]}`. Not connected → an error naming `finance.connect`.
- `finance.sync {force?}` → `{new, accounts_synced, accounts_skipped, notes}`. Call it before answering about "today"; `force` only on request (banks allow about 4 calls a day). `notes` says when a bank rate-limited or a consent expired: repeat it.
- `finance.transactions {since?, until?, account?, category?, query?, limit?}` → `{count, transactions: [{id, account, date, amount, currency, label, counterparty, category, shared, paid_by}]}`, newest first. Dates `YYYY-MM-DD`.
- `finance.summary {month?}` (`YYYY-MM`, default current) → `{month, income, expenses, by_category: [{category, spent, budget, ratio}], biggest: [{date, label, amount, category}], vs_previous: {month, expenses, change_pct}}`. `ratio` is spent/budget (null without budget).
- `finance.recurring {}` → `{recurring: [{name, amount, period: monthly|yearly, category, last, next}], monthly_total}`: subscriptions and charges seen 3+ times at regular intervals.
- `finance.settle {month?}` → `{month, total, count, people: [{name, paid, share, balance}], transfers: [{from, to, amount}]}`: shared expenses split equally among `household` (finance.toml). Say the transfers ("Marie doit 84 € à Jean-Baptiste").
- `finance.networth {}` → `{total, currency, by_kind: [{kind, value, share}], lines: [{name, kind, value, quantity?, ticker?}], notes}`: bank balances plus declared holdings priced now; `notes` lists lines that could not be priced.
- `finance.banks {query?}` → `{banks: [{name, country, consent_days}]}` (French banks).
- Reversible: `finance.categorize {category, pattern?|transaction_id?}` (pattern = substring or regex on label/counterparty, becomes a rule), `finance.budget_set {category, amount}` (0 removes), `finance.shared {transaction_id?|pattern?, shared?, paid_by?}`, `finance.holding_set {name, kind: etf|stock|crypto|scpi|av|per|cash|real_estate|other, ticker?, quantity?, value?, currency?}` (ticker = Yahoo symbol like `CW8.PA`, `AAPL`; CoinGecko id like `bitcoin` for crypto; `value` for what has no quote: SCPI, assurance-vie, PER, real estate), `finance.holding_remove {name}`.
- Linking: `finance.connect {bank}` → `{url, next}`: give the url and say `next` word for word; the user opens it on the phone, validates at the bank, then pastes the landing page's address. Then `finance.connected {url}` → `{session_id, bank, accounts, valid_until}`; follow with `finance.sync {}`. `finance.disconnect {session_id}` (confirm) closes the consent.

Answering:
- A number or a short list ("combien en restaurants ?", "mes abonnements") gets text with the real figures and the month; name the category and the period you used.
- "Où en est mon budget", "mon patrimoine", "montre mes dépenses" get a `window` view with Metrics and a Table (example below): call the tool first, use real values as Query defaults.
- Never invent or round-trip a figure that isn't in a result; never guess a category: say `autre` and offer `finance.categorize`.
- Not configured (an error naming finance.toml or setup.md): read `references/setup.md` with `read_skill {name: "finance", file: "references/setup.md"}` and walk the user through it. Consent expired: offer `finance.connect` again.
- A rate-limit note: say the figures are from the last sync and when the next one is possible. Don't retry with `force`.

Example: the month's budget as a window.

```
root = Surface("window", [head, table], "Budget du mois")
s = Query("finance.summary", {}, {month: "", income: 0, expenses: 0, by_category: [], vs_previous: {expenses: 0, change_pct: 0}})
head = Row([Metric("Revenus", @Round(s.income) + " €"), Metric("Dépenses", @Round(s.expenses) + " €"), Metric("vs mois dernier", s.vs_previous.change_pct + " %")])
table = Table(["Catégorie", "Dépensé", "Budget"], @Each(s.by_category, "c", [c.category, @Round(c.spent) + " €", c.budget == null ? "—" : @Round(c.budget) + " €"]))
```

Net worth: the same shape with `finance.networth` (`Metric("Patrimoine", @Round(n.total) + " €")`, a Table of `n.lines` with name, kind, value).

## Watching

Two ready-made setups (`recipe.list`): `finance-budget` (every evening, an alert when a category passes 80 % of its budget or the month runs 20 % over the previous one) and `finance-watch` (an alert per notable movement: big charge, new recurring debit, salary, odd amount). Offer them after the user sets budgets or asks to be warned.
