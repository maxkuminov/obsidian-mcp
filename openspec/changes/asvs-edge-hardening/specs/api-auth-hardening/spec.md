## MODIFIED Requirements

### Requirement: Login rate limiting
The `POST /admin/auth/login` endpoint SHALL be rate-limited to 5 attempts per minute per IP address using slowapi, and SHALL additionally enforce a per-username failed-attempt budget keyed on the submitted username. The two limits are additive and neither replaces the other: an address-keyed limit hands an attacker a fresh allowance for every address they can forge, and a username-keyed limit alone lets one address walk many accounts.

The username budget SHALL count only failures, SHALL be consulted before the password is compared, and SHALL expire on its own within a short window. It MUST NOT be an account lockout: nothing durable is written, there is no administrative unlock, an already-authenticated session is unaffected, and the budget clears itself once the window passes without further failures. The threshold SHALL sit far above a human typo rate and far below an online guessing budget.

The refusal produced by the username budget SHALL be byte-identical to an ordinary failed login — the same status, headers and rendered body as an unknown username, an inactive account or a wrong password. A distinguishable response would confirm which usernames exist and which are currently under attack. The distinction SHALL exist only in the security log.

#### Scenario: Rate limit enforced on login
- **WHEN** a client submits more than 5 login attempts within one minute from the same IP
- **THEN** subsequent attempts SHALL receive HTTP 429

#### Scenario: Normal login not affected
- **WHEN** a client submits fewer than 5 login attempts within one minute
- **THEN** login attempts SHALL proceed normally

#### Scenario: Rotating addresses cannot outrun the username budget
- **WHEN** failed login attempts for one username exceed the configured budget within its window, each arriving from a different client address
- **THEN** further attempts for that username SHALL be refused without the submitted password being compared

#### Scenario: The throttled refusal is not a username oracle
- **WHEN** a login attempt is refused by the username budget
- **THEN** its status, headers and body SHALL be identical to the response for an ordinary failed login, and it SHALL NOT be a 429

#### Scenario: A different username is unaffected
- **WHEN** one username's budget is exhausted
- **THEN** a login attempt for a different username from the same address SHALL still be evaluated, subject only to the address limit

#### Scenario: Success does not consume the budget
- **WHEN** a user signs in successfully
- **THEN** no failure SHALL be recorded against that username

#### Scenario: An authenticated session is unaffected by a flood
- **WHEN** a username's budget is exhausted and the owner of that account holds a valid panel session
- **THEN** that session SHALL continue to work and the owner SHALL NOT be signed out

#### Scenario: The budget expires without intervention
- **WHEN** the configured window passes with no further failed attempt for that username
- **THEN** attempts for it SHALL be evaluated normally again, with no administrative action required

#### Scenario: The budget is bounded in memory
- **WHEN** attempts arrive for an unbounded number of distinct submitted usernames
- **THEN** the budget's state SHALL occupy a fixed, configured number of slots rather than growing with the number of usernames seen

#### Scenario: A submitted username cannot be unbounded
- **WHEN** a login form is submitted with a username longer than the `users.username` column width
- **THEN** the value SHALL be truncated to that width before it is used as a budget key or written to any record
