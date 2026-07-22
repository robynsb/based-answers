**This software is 90% vibe-coded using Claude Code w/ Opus 4.8/Fable 5 and Opencode w/ Deepseek V4 Flash. I make no guarantees on the code quality.**

# Based Answers

This is an agent harness for answering questions about PDFs which are treated as ground-truth with an emphasis on exact citations and human-in-the-loop verification.

This is kind of a token-maxxing RAG.

## Demo

https://github.com/user-attachments/assets/48c34111-0034-4531-8ed1-551b14fb9da4

https://github.com/user-attachments/assets/8dde70d9-4ac3-4368-bde0-9e5cf685435a

## How it works

In my experience, LLM tools that cite its claims (such as Google AI) freuqnelty make claims not backed up by their citations. This project is an idea for preventing this issue.

The idea is that a searcher agent must generate an answer yaml file (using a tool) that structurally requires citations for each claim

Example:

```yaml
question: "C function to dealloc state machines in PIO RP2040?"
answers:
  - claim: "The function pio_sm_unclaim(PIO pio, uint sm) marks a previously claimed state machine as no longer used."
    citations:
      - text: "void pio_sm_claim (PIO pio, uint sm)\nMark a state machine as used.\nvoid pio_claim_sm_mask (PIO pio, uint sm_mask)\nMark multiple state machines as used.\nvoid pio_sm_unclaim (PIO pio, uint sm)\nMark a state machine as no longer used.\nint pio_claim_unused_sm (PIO pio, bool required)\nClaim a free state machine on a PIO instance.\nbool pio_sm_is_claimed (PIO pio, uint sm)\nDetermine if a PIO state machine is claimed.\nbool pio_claim_free_sm_and_add_program (const pio_program_t *program, PIO *pio, uint *sm, uint *offset)\nFinds a PIO and statemachine and adds a program into PIO memory.\nbool pio_claim_free_sm_and_add_program_for_gpio_range (const pio_program_t *program, PIO *pio, uint *sm, uint *offset,\nuint gpio_base, uint gpio_count, bool set_gpio_base)\nFinds a PIO and statemachine and adds a program into PIO memory.\nvoid pio_remove_program_and_unclaim_sm (const pio_program_t *program, PIO pio, uint sm, uint offset)\nRemoves a program from PIO memory and unclaims the state machine."
        page: 226
        source: "RP-009085-KB-1-raspberry-pi-pico-c-sdk.pdf"
  - claim: "The function pio_remove_program_and_unclaim_sm(const pio_program_t *program, PIO pio, uint sm, uint offset) removes a program from PIO instruction memory and unclaims the state machine, freeing both resources."
    citations:
      - text: "void pio_remove_program_and_unclaim_sm (const pio_program_t * program, PIO pio, uint sm, uint offset)\nRemoves a program from PIO memory and unclaims the state machine.\nParameters\nprogram\nPIO program to remove from memory\npio\nPIO hardware instance being used\nsm\nPIO state machine that was claimed\noffset\noffset of the program in PIO memory\nSee also\npio_claim_free_sm_and_add_program"
        page: 235
        source: "RP-009085-KB-1-raspberry-pi-pico-c-sdk.pdf"
```

Then sub-agents are spawned for each claim and have no idea about the top-level question being asked and are tasked to decide whether the citations imply the claim.

If failed, the searcher agent receives feedback and we continue in a loop.

## A way to run this
The software is not very complicated, you can _probably_ ask your favorite coding agent to run it for you.

### With Nix
```
# Git clone repository 
export PATH_TO_BASED_ANSWERS=(INSERT ABSOLUTE PATH TO REPO HERE)
# Navigate to a directory with many PDFs you want to ask about.
nix develop "path:$PATH_TO_BASED_ANSWERS" -c python3 $PATH_TO_BASED_ANSWERS/based-answers.py
```

### With Docker
Coming soon...
