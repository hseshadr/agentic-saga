@offline @bdd @temporal
Feature: A checkout Saga keeps every external change safe

  Scenario: A healthy checkout completes once
    Given a healthy checkout provider
    When the agent pursues the checkout goal through a Temporal Saga
    Then the Temporal Saga finishes as "succeeded_verified"
    And reservation, payment, and fulfillment each happen once
    And the final order is proven by one authoritative read
    And no compensation is needed

  Scenario: A fulfillment rejection reverses completed work
    Given fulfillment rejects the order after accepting it
    When the agent pursues the checkout goal through a Temporal Saga
    Then the Temporal Saga finishes as "compensated_verified"
    And compensation happens in the order "cancel_fulfillment,refund_payment,release_inventory"
    And each completed external change is compensated once

  Scenario: A lost payment response does not charge twice
    Given payment succeeds but its response is lost
    When the agent pursues the checkout goal through a Temporal Saga
    Then the Temporal Saga finishes as "succeeded_verified"
    And payment has 2 execution attempts, 1 effect, and 1 reconciliation
    And fulfillment continues only after payment is confirmed

  Scenario: An uncertain refund pauses for an authorized person
    Given fulfillment rejects and the refund response is uncertain
    When the agent pursues the checkout goal through a Temporal Saga
    Then the Saga pauses in "human_required"
    And a stale human decision is rejected
    And an unauthorized human decision is rejected
    And a valid human decision resumes compensation
    And the Temporal Saga finishes as "compensated_verified"
    And compensation happens in the order "cancel_fulfillment,refund_payment,release_inventory"
    And provider "refund_payment" reports 2 executions, 1 effects, and 1 reconciliations
