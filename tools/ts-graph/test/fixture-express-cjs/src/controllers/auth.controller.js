const catchAsync = (fn) => fn;

const login = catchAsync(async (req, res) => {
  res.send(token());
});

function token() {
  return "t";
}

module.exports = { login };
