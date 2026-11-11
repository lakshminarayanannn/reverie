# Purpose: benchmark mode (task contracts and environment rules)

This memory is generated from a corpus of past task attempts, each ending with a verifier result (PASS or FAIL), and is given to a fresh agent that will attempt related but different tasks in similar environments.

It should capture what the verifier actually rewards and punishes (output locations, formats, naming, required checks), rules about the environment that held across attempts (installed tools, versions, missing dependencies and how to get around them, commands that fail), and methods that separated passing attempts from failing ones. Prefer lessons backed by a contrast between a failing and a passing attempt.

It must not contain final answers, submitted artifacts, literal patches, or commands that reproduce a prior final output. Critical facts (paths, commands, formats, version strings) stay verbatim.
