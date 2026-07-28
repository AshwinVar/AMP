import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Unit tests for the shared hooks in lib/. Deliberately narrow: this is not a
// component-rendering harness, it is a place to pin the behaviour of logic that
// was previously only provable by driving a browser by hand (see #383, #385).
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    include: ["lib/**/*.test.{ts,tsx}"],
    globals: true,
  },
});
