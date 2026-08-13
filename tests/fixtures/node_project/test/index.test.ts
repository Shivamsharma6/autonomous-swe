import { expect, test } from "vitest";

import { answer } from "../src/index";

test("answer", () => expect(answer()).toBe(42));

