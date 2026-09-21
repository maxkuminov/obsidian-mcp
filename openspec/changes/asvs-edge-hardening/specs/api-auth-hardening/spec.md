## MODIFIED Requirements

### Requirement: Login rate limiting
The `POST /admin/auth/login` endpoint SHALL be rate-limited to 5 attempts per minute per IP address using slowapi, and SHALL additionally enforce a per-account failed-attempt budget keyed on the account the submitted username resolves to. The two limits are additive and neither replaces the other: an address-keyed limit hands an attacker a fresh allowance for every address they can forge, and an account-keyed limit alone lets one address walk many accounts.

The account budget SHALL be keyed **exactly**, by the `users` row identifier, and MUST NOT share a counter between two accounts under any input. A budget that merged keys would let failures aimed at one name refuse a different account's correct password, which is a cross-account denial rather than the accepted single-account one. A submitted username that resolves to no account SHALL consume no account budget and SHALL be governed by the address limit alone, because there is no credential behind such a name to guess.

The budget SHALL count only failures, SHALL be consulted before the submitted password is compared, and SHALL expire on its own within a short window. It MUST NOT be an account lockout: nothing durable is written, there is no administrative unlock, an already-authenticated session is unaffected, and the counter clears itself once the window passes without further failures. The threshold SHALL sit far above a human typo rate and far below an online guessing budget.

The refusal produced by the account budget SHALL be equivalent in content to an ordinary failed login — the same HTTP status, the same rendered template and the same user-visible message for the same submitted inputs. Equivalence is defined over response content only. It explicitly **excludes** response timing, which already differs between existing cases because the password comparison runs only for an active account, and **excludes** per-response nondeterministic values such as a CSRF token or a session cookie. The remaining distinction SHALL exist only in the security log.

#### Scenario: Rate limit enforced on login
- **WHEN** a client submits more than 5 login attempts within one minute from the same IP
- **THEN** subsequent attempts SHALL receive HTTP 429

#### Scenario: Normal login not affected
- **WHEN** a client submits fewer than 5 login attempts within one minute and the target account's failure budget is unexhausted
- **THEN** login attempts SHALL proceed normally

#### Scenario: Rotating addresses cannot outrun the account budget
- **WHEN** failed login attempts for one existing account exceed the configured budget within its window, each arriving from a different client address
- **THEN** further attempts for that account SHALL be refused without the submitted password being compared

#### Scenario: A different account is unaffected
- **WHEN** one account's budget is exhausted
- **THEN** an attempt for any other account SHALL still be evaluated on its own counter, subject only to the address limit
- **AND** no input to the exhausted account's budget SHALL be able to consume another account's allowance

#### Scenario: An unknown username consumes no account budget
- **WHEN** login attempts are submitted for usernames that resolve to no account
- **THEN** no account budget SHALL be consumed or created for them
- **AND** an existing account's correct password SHALL still be accepted however many such attempts were made

#### Scenario: The budget is bounded by the number of accounts
- **WHEN** attempts arrive for an unbounded number of distinct submitted usernames
- **THEN** the budget SHALL hold at most one counter per existing account that has recently failed, and SHALL NOT grow with the number of usernames seen

#### Scenario: The throttled refusal is not distinguishable by content
- **WHEN** a login attempt is refused by the account budget
- **THEN** its status, rendered template and user-visible message SHALL be identical to those of an ordinary failed login for the same inputs, and it SHALL NOT be a 429

#### Scenario: Success does not consume the budget
- **WHEN** a user signs in successfully
- **THEN** no failure SHALL be recorded against that account

#### Scenario: An authenticated session is unaffected by a flood
- **WHEN** an account's budget is exhausted and the owner of that account holds a valid panel session
- **THEN** that session SHALL continue to work and the owner SHALL NOT be signed out

#### Scenario: The budget expires without intervention
- **WHEN** the configured window passes with no further failed attempt for that account
- **THEN** attempts for it SHALL be evaluated normally again, with no administrative action required
