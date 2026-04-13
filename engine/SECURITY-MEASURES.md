# Security Measures and Capital Protection Architecture

## Executive Summary

This trading stack is designed so that return generation and capital protection are treated as two separate engineering problems.

- The trading framework is responsible for signal generation, portfolio construction, and execution discipline.
- Claude Code is responsible for controlled remote operation, approvals, messaging access, and operational containment.

For investors, that distinction matters. A profitable strategy can still be unsafe if credentials are exposed, if unauthorized users can issue instructions, or if external prompts can trigger unintended actions. The security architecture is therefore designed to reduce both technical compromise risk and operator-error risk.

## Core Security Principle

The system is built on least privilege and explicit control boundaries:

- only the owner’s approved chat identities may interact with Claude Code
- the trading engine is controllable remotely, but still stateful and auditable
- risky host capabilities are denied at the gateway layer
- execution commands require confirmations and operate inside a constrained workspace model
- secrets are separated from normal project code and chat flows

This is not “security by obscurity.” It is layered operational security.

## Communication and Access Control

### Owner-Only Messenger Control

The Claude Code environment is configured around explicit owner access paths through Telegram and Discord.

Current controls include:

- Telegram direct-message pairing
- Telegram sender allowlisting
- Discord direct-message pairing
- Discord sender allowlisting
- guild-level user allowlisting on Discord
- mention-required behavior in the configured Discord guild
- bot-authored messages rejected
- Discord voice ingestion disabled

The security goal is straightforward:

- no one other than the approved owner identity should be able to issue operational instructions
- ambient group chat chatter should not be treated as actionable control input

This materially reduces the risk of accidental execution from shared channels, compromised bots, or conversational noise.

## Claude Code Gateway Security

The Claude Code gateway is configured for local-mode operation with token authentication and loopback binding.

That means:

- the control surface is not intentionally exposed as a public network service
- authenticated access is required
- remote access is intended to happen through safer overlays such as SSH or Tailscale rather than open internet exposure

In practice, this supports secure phone and remote-machine control without turning the trading host into an unauthenticated internet endpoint.

## Plugin and Extension Trust Controls

Plugin trust is explicitly pinned through an allowlist of approved plugin IDs.

Why this matters:

- local extensions are executable code
- untracked auto-loaded plugins expand attack surface
- any plugin that can observe or influence runtime should be explicitly trusted, not discovered opportunistically

Restricting the plugin set narrows the system to the minimum required operational surface.

## Execution Safety and Approval Controls

The Claude Code tools profile is configured with:

- full exec security
- `ask: always`
- workspace-only filesystem access

The effect is that command execution is not meant to run silently as an unconstrained shell. The agent operates under an approval model rather than a “do anything by default” model.

This matters especially for a trading environment because the dangerous failure modes are not just malware-style compromise. They also include:

- unintended file reads
- accidental state mutations
- command injection from external content
- automated exfiltration of credentials

The security posture is therefore operationally conservative by design.

## Secret Management

### Separation of Secrets from Strategy Code

The strategy code reads exchange credentials from configuration and environment sources rather than hardcoding them into the algorithms themselves. This keeps alpha logic and operational secrets conceptually separate.

### Keychain-Based Secret Provider

Claude Code is configured to support a Keychain-backed secret provider through an exec-based secret retrieval command. This means the intended end-state is:

- credentials stored in macOS Keychain
- runtime retrieval through the configured secrets provider
- no need for broad plaintext duplication throughout the workspace

That is a materially stronger design than relying on scattered plain text secrets across project files.

### Why This Protects Investor Capital

If strategy logic is valuable, credentials are even more sensitive. Exchange compromise does not require defeating the trading model. It only requires obtaining the keys.

Separating and hardening credential access reduces the probability that a messaging mistake, transcript leak, or local plugin issue becomes an account-level event.

## Device and Session Security

Claude Code uses paired-device identity and operator tokens for approved control surfaces. That creates an auditable pairing model instead of implicit trust.

The security value is:

- known devices are enumerated
- operator roles and scopes are explicit
- unauthorized devices do not automatically inherit administrative control

This reduces casual lateral access and makes session trust a managed concept rather than a side effect of being “on the same machine.”

## Trading-Host Operational Security

The deployment pattern is designed around a single workspace and explicit state files rather than a large number of background processes.

Advantages:

- state is inspectable
- backups are straightforward
- emergency stop logic is clear
- reconciliation is explicit
- operational surprises are reduced

The live engine persists state atomically and maintains backup state snapshots. That improves recoverability if a run fails mid-cycle or if the operator needs to inspect the last known state.

## Capital Safety in the Trading Engine

Security is not only about preventing hackers. It is also about preventing the strategy from damaging investor capital through uncontrolled behavior.

### 1. Live / Paper Separation

The broker is designed with paper-trading behavior by default and a separate live path when explicitly enabled.

That separation is essential. It allows:

- infrastructure testing without market risk
- operational rehearsal of rebalances and reports
- validation of state transitions and notifier behavior before live capital is exposed

### 2. State Reconciliation

Before rebalancing, the engine reconciles internal state against exchange state and treats the exchange as source of truth.

This protects against:

- stale local assumptions
- partial previous failures
- mismatch between expected and actual positions
- accidental double-sizing after interrupted runs

### 3. Portfolio Constraints

Capital protection is embedded into the portfolio layer through:

- gross leverage caps
- net exposure caps
- single-position caps
- minimum rebalance thresholds

These controls limit damage from both model error and operational error.

### 4. Dynamic Risk Controls

The trading engine uses:

- ATR-based stops
- drawdown circuit breakers
- volatility regime scaling
- correlation watchdog logic

This is security in the capital-preservation sense: the system contains its own behavior when market conditions become structurally hostile.

## Protection Against Prompt Injection and External Manipulation

One of the unique risks in AI-operated systems is instruction contamination from external content.

Potential examples include:

- an X post telling the system to buy or sell
- a Discord message instructing the system to transfer funds
- a forwarded screenshot that embeds malicious operational text
- a web page or email containing adversarial instructions

The control philosophy here is that external content is not trusted authority.

Security protections include:

- explicit owner access restrictions on messenger channels
- command approvals rather than silent unrestricted execution
- denial of sensitive node commands at the gateway layer
- separation between messaging control and exchange execution logic
- operator-visible reporting rather than hidden autonomous action

Most importantly, the trading engine itself is built around explicit commands such as `start`, `rebalance`, `status`, and `stop`. It is not structured as “any text from the internet can directly call exchange order methods.”

That architecture sharply reduces the risk that social or web content can become an unauthorized financial instruction.

## Phone-Based Operation Without Public Exposure

A key operational requirement is remote supervision from a phone.

The secure design is:

- use private messaging surfaces already bound to owner identity
- keep the gateway local/authenticated
- reach the machine through controlled overlays, not open public endpoints
- use human-readable status and rebalance reports rather than opaque raw logs

This gives the operator the ability to:

- inspect NAV and exposure
- review executed trades
- check risk state
- stop the engine if needed

without creating a broad remote attack surface.

## Auditability and Transparency

The system is built to produce operational artifacts rather than hidden state.

Available audit layers include:

- live state file
- trade log
- NAV history
- rebalance reports
- risk summaries
- reconciliation warnings
- notifier messages suitable for operator review

For investors, this matters because capital safety is not only about controls being present. It is about controls being reviewable.

## Why Investor Money Is Safer in This Design

Investor capital is protected by multiple independent layers:

1. Unauthorized instruction risk is reduced through owner-only messenger control.
2. Credential exposure risk is reduced through secret separation and Keychain-backed design.
3. Host misuse risk is reduced through approval-based execution and gateway restrictions.
4. Runtime expansion risk is reduced through plugin allowlisting.
5. Trading-loss escalation risk is reduced through portfolio caps and dynamic circuit breakers.
6. State drift risk is reduced through exchange reconciliation and persistent auditable state.
7. Remote operating risk is reduced through secure phone-accessible control paths rather than public blind exposure.

No single measure is sufficient. Together, they form a practical defense-in-depth model.

## Honest Framing for Investors

This system should not be presented as “unhackable” or “risk free.” No serious technical operator should make that claim.

The more credible statement is:

- the strategy is designed to pursue return systematically
- the operational environment is designed to reduce preventable failure modes
- the security model assumes that messaging systems, machines, and markets can all fail in different ways
- the architecture therefore relies on layered controls, constrained execution, explicit reporting, and recoverable state

That is the right standard for investor trust.

## Closing Statement

The purpose of this security architecture is not cosmetic compliance. It is to preserve control of capital, execution, and information under real operating conditions.

In investor terms:

- the trading system is how returns are pursued
- the security system is how the right people stay in control of the machine, the keys, and the money

That combination is what makes the platform investable.
