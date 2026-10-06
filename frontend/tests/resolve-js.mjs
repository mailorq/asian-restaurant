// the app imports its own modules without an extension, as the bundler resolves them; tsc keeps the
// specifiers as written, so node gets the .js of a relative one here
export async function resolve(specifier, context, next) {
  if (/^\.{1,2}\//.test(specifier) && !/\.[cm]?js$/.test(specifier)) {
    return next(`${specifier}.js`, context);
  }
  return next(specifier, context);
}
