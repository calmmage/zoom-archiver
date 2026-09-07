# Social preview

`social-preview.png` is a 1280 × 640 screenshot of actual `zoom-archiver --help`
output with the pixel-art logo. Same image: `docs/examples/hero.png`. It contains no
credentials, account data, archive paths, or example download results.

Keeping the file in this directory does not configure GitHub's social preview.
GitHub's documented repository-update API has no social-preview upload field;
the image must be uploaded through the repository settings.
In the repository's **Settings → General → Social preview**, choose **Edit →
Upload an image** and select `social-preview.png`.

GitHub documents initial uploads for public repositories; a private repository
can update an image previously uploaded. See [GitHub's social-preview guide](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/customizing-your-repositorys-social-media-preview).
