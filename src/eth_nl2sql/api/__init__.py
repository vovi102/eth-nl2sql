"""The web app: an HTTP API over the pipeline, and the page of web/ (`eth-nl2sql serve`).

Nothing here changes the pipeline: a question goes through the same `EntityLinker`,
`PromptBuilder`, `generate_one` and `SqlRunner` as `generate-sql`, so a dev question asked
live gives the SQL of the frozen runs (`eth-nl2sql check-serve` checks it).
"""
