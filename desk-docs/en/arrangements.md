# Arrangements

The Arrangements sidebar has two sections, "Local" and "System". "Local" manages execution rules created by the user; "System" shows Desk's background maintenance jobs. An arrangement decides when something runs, and the results are stored on this machine.

## Creating and running

In "Arrangements → Local", choose New; Desk can help you create it, or you can set it up by hand. Describe what to run, choose the identity that runs it, then set the time. The default identity is Desktron; you can also use the execution settings of an existing Feishu Bot.

Supported schedules are manual only, a specific date and time, a fixed interval in minutes, daily, working days, weekly, and custom repetition. Custom repetition sets an interval in days, weeks, or months and a date range. Times are calculated in this machine's time zone.

You can do a trial run before saving; after saving you can run it immediately, pause, resume, or delete it. A trial run does not create a permanent arrangement for the user. A one-time arrangement that has expired needs a new date before it can be enabled again.

## Results and failures

View the status, result, and errors of each run. Binding a Bot only reuses its execution settings; it does not set up triggering by Feishu messages, and it does not send results to Feishu automatically.

Arrangements are scheduled by the local Desk process. They cannot trigger normally while the process is not running; records still marked as running after a restart are recorded as interrupted. The latest 200 runs are kept, and this is not a permanent audit store.

System arrangements each have their own maintenance purpose and controls; do not infer their behavior from the time rules of ordinary arrangements. For memory maintenance, see [Knowledge, skills, and memory](knowledge-memory.md).
