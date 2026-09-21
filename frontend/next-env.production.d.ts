/// <reference types="next" />
/// <reference types="next/image-types/global" />
import "./.next/types/routes.d.ts";

// This stable shim keeps production type checking independent of Next's
// generated root next-env.d.ts, which QA/dev profiles may rewrite for another
// distDir while a production build is running.
