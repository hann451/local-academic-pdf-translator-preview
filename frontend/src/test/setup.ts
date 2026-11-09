import "@testing-library/jest-dom/vitest";

if (typeof URL.createObjectURL !== "function") {
  URL.createObjectURL = () => "blob:http://127.0.0.1:5173/default-blob";
}
if (typeof URL.revokeObjectURL !== "function") {
  URL.revokeObjectURL = () => {};
}
