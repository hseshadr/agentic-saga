@offline @bdd
Feature: An agent proposes work while the Saga kernel owns transaction safety

  Scenario: The happy path completes from dynamic tool proposals
    Given the "happy-path" ecommerce scenario
    When the offline agent pursues the order goal
    Then the saga state is "succeeded_verified"
    And the agent proposal sequence is "check_inventory,reserve_inventory,charge_payment,schedule_fulfillment,inspect_order,finish"
    And provider "reserve_inventory" has 1 execute, 1 effect, and 0 reconciliation calls
    And provider "charge_payment" has 1 execute, 1 effect, and 0 reconciliation calls
    And provider "schedule_fulfillment" has 1 execute, 1 effect, and 0 reconciliation calls
    And the timeline records intent before effect and fresh proof before terminal state

  Scenario: A failed business goal compensates confirmed effects in reverse order
    Given the "business-failure" ecommerce scenario
    When the offline agent pursues the order goal
    Then the saga state is "compensated_verified"
    And the compensation sequence is "cancel_fulfillment,refund_payment,release_inventory"
    And every forward and compensation provider has 1 execute, 1 effect, and 0 reconciliations
    And the timeline records the agent compensation request and verified completion

  Scenario: A lost provider response reconciles after restart without duplication
    Given the "lost-response" ecommerce scenario
    When the offline agent pursues the order goal
    Then the saga state is "succeeded_verified"
    And the Saga runtime restarted from durable state
    And provider "charge_payment" has 1 execute, 1 effect, and 2 reconciliation calls
    And the timeline records reconciliation before later forward effects

  Scenario: Unverifiable compensation stops for a human
    Given the "compensation-failure" ecommerce scenario
    When the offline agent pursues the order goal
    Then the saga state is "human_required"
    And provider "refund_payment" has 1 execute, 1 effect, and 1 reconciliation calls
    And the escalation packet identifies "reconciliation_unsafe"
    And no effect occurs after human escalation
