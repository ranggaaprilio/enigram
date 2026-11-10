// Image imports resolve to their URL (webpack asset modules).
declare module '*.png' {
  const src: string;
  export default src;
}
