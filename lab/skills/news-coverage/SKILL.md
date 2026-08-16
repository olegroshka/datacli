---
name: news-coverage
summary: How much news a ticker/issuer gets, how it compares to peers, and whether the vendor sentiment says anything.
inputs: [ticker]
tier: cheap
---

Goal
Characterise the news flow for one ticker (or lane) using the derived panels, and
sanity-check the vendor sentiment against volume, so a later model does not
mistake tagging artefacts for signal.

Approach (write your own read-only SQL; these are starting points)
- Prefer the issuer grain for UK/EU names (home-exchange tags under-count):
    SELECT date, n_articles, share_of_day, n_solo, n_symbols, polarity_mean, pos_share, neg_share
    FROM news_issuer_daily WHERE ticker = ? AND exchange = ? ORDER BY date DESC LIMIT 60
- Compare with the symbol grain to see the gap:
    SELECT sum(n_articles) FROM news_daily WHERE ticker = ? AND exchange = ? AND date >= current_date - 30
- Normalise: use share_of_day (volume is not stationary -- 2019/2020 and 2024 are thin
  years) and n_solo (articles genuinely about the company, <= 3 tags) rather than
  raw counts. Look for tagging bursts (share_of_day > 0.15 on a day).
- If our own scores exist (news_scores_event, symbol IS NULL for article-level rows):
    SELECT event_type, count(*), round(avg(sentiment), 2) FROM news_scores_event s
    JOIN news n USING (article_id) WHERE list_contains(n.symbols, ?) AND s.symbol IS NULL
    GROUP BY 1 ORDER BY 2 DESC
  and compare our sentiment with the vendor polarity_mean for the same days.

Output
Volume over the last 60 days (issuer vs symbol grain), the share of solo articles,
the busiest days with what drove them, vendor polarity vs our score if present, and
one line saying whether the news flow for this name is dense enough to model daily.
